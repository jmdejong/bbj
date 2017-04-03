from src.exceptions import BBJException, BBJParameterError, BBJUserError
from src import db, schema
from functools import wraps
from uuid import uuid1
import traceback
import cherrypy
import sqlite3
import json

dbname = "data.sqlite"

# user anonymity is achieved in the laziest possible way: a literal user
# named anonymous. may god have mercy on my soul.
with sqlite3.connect(dbname) as _c:
    db.anon = db.user_resolve(_c, "anonymous")
    if not db.anon:
        db.anon = db.user_register(
            _c, "anonymous", # this is the hash for "anon"
            "5430eeed859cad61d925097ec4f53246"
            "1ccf1ab6b9802b09a313be1478a4d614")


# creates a database connection for each thread
def db_connect(_):
    cherrypy.thread_data.db = sqlite3.connect(dbname)
cherrypy.engine.subscribe('start_thread', db_connect)


def api_method(function):
    """
    A wrapper that handles encoding of objects and errors to a
    standard format for the API, resolves and authorizes users
    from header data, and prepares the arguments for each method.

    In the body of each api method and all the functions
    they utilize, BBJExceptions are caught and their attached
    schema is dispatched to the client. All other unhandled
    exceptions will throw a code 1 back at the client and log
    it for inspection. Errors related to JSON decoding are
    caught as well and returned to the client as code 0.
    """
    function.exposed = True
    @wraps(function)
    def wrapper(self, *args, **kwargs):
        response = None
        try:
            # read in the body from the request to a string...
            body = str(cherrypy.request.body.read(), "utf8")
            # is it just empty bytes? not all methods require an input
            if body:
                body = json.loads(body)
                if isinstance(body, dict):
                    # lowercase all of its top-level keys
                    body = {str(key).lower(): value for key, value in body.items()}

            username = cherrypy.request.headers.get("User")
            auth = cherrypy.request.headers.get("Auth")

            if (username and not auth) or (auth and not username):
                return json.dumps(schema.error(5,
                    "User or Auth was given without the other."))

            elif not username and not auth:
                user = db.anon

            else:
                user = db.user_resolve(cherrypy.thread_data.db, username)
                if not user:
                    raise BBJUserError("User %s is not registered" % username)

                if auth != user["auth_hash"]:
                    return json.dumps(schema.error(5,
                        "Invalid authorization key for user."))

            # api_methods may choose to bind a usermap into the thread_data
            # which will send it off with the response
            cherrypy.thread_data.usermap = {}
            value = function(self, body, cherrypy.thread_data.db, user)
            response = schema.response(value, cherrypy.thread_data.usermap)

        except BBJException as e:
            response = e.schema

        except json.JSONDecodeError as e:
            response = schema.error(0, str(e))

        except Exception as e:
            error_id = uuid1().hex
            response = schema.error(1,
                "Internal server error: code {} {}"
                    .format(error_id, repr(e)))
            with open("logs/exceptions/" + error_id, "a") as log:
                traceback.print_tb(e.__traceback__, file=log)
                log.write(repr(e))
            print("logged code 1 exception " + error_id)

        finally:
            return json.dumps(response)

    return wrapper


def create_usermap(connection, obj):
    """
    Creates a mapping of all the user_ids that occur in OBJ to
    their full user objects (names, profile info, etc). Can
    be a thread_index or a messages object from one.
    """

    return {
        user_id: db.user_resolve(
            connection,
            user_id,
            externalize=True,
            return_false=False)
        for user_id in {item["author"] for item in obj}
    }



def validate(json, args):
    """
    Ensure the json object contains all the keys needed to satisfy
    its endpoint (and isnt empty)
    """
    if not json:
        raise BBJParameterError(
            "JSON input is empty. This method requires the following "
            "arguments: {}".format(", ".join(args)))

    for arg in args:
        if arg not in json.keys():
            raise BBJParameterError(
                "Required parameter {} is absent from the request. "
                "This method requires the following arguments: {}"
                .format(arg, ", ".join(args)))


class API(object):
    """
    This object contains all the API endpoints for bbj.
    The html serving part of the server is not written
    yet, so this is currently the only module being
    served.
    """
    @api_method
    def user_register(self, args, database, user, **kwargs):
        """
        Register a new user into the system and return the new object.
        Requires the string arguments `user_name` and `auth_hash`.
        Do not send User/Auth headers with this method.
        """
        validate(args, ["user_name", "auth_hash"])
        return db.user_register(
            database, args["user_name"], args["auth_hash"])


    @api_method
    def user_update(self, args, database, user, **kwargs):
        """
        Receives new parameters and assigns them to the user_object
        in the database. The following new parameters can be supplied:
        `user_name`, `auth_hash`, `quip`, `bio`, and `color`. Any number
        of them may be supplied.

        The newly updated user object is returned on success.
        """
        validate(args, []) # just make sure its not empty
        return db.user_update(database, user, args)


    @api_method
    def get_me(self, args, database, user, **kwargs):
        """
        Requires no arguments. Returns your internal user object,
        including your authorization hash.
        """
        return user


    @api_method
    def user_get(self, args, database, user, **kwargs):
        """
        Retreive an external user object for the given `user`.
        Can be a user_id or user_name.
        """
        validate(args, ["user"])
        return db.user_resolve(
            database, args["user"], return_false=False, externalize=True)


    @api_method
    def thread_index(self, args, database, user, **kwargs):
        """
        Return an array with all the threads, ordered by most recent activity.
        Requires no arguments.
        """
        threads = db.thread_index(database)
        cherrypy.thread_data.usermap = create_usermap(database, threads)
        return threads


    @api_method
    def thread_create(self, args, database, user, **kwargs):
        """
        Creates a new thread and returns it. Requires the non-empty
        string arguments `body` and `title`
        """
        validate(args, ["body", "title"])
        thread = db.thread_create(
            database, user["user_id"], args["body"], args["title"])
        cherrypy.thread_data.usermap = thread
        return thread


    @api_method
    def thread_reply(self, args, database, user, **kwargs):
        """
        Creates a new reply for the given thread and returns it.
        Requires the string arguments `thread_id` and `body`
        """
        validate(args, ["thread_id", "body"])
        return db.thread_reply(
            database, user["user_id"], args["thread_id"], args["body"])


    @api_method
    def thread_load(self, args, database, user, **kwargs):
        """
        Returns the thread object with all of its messages loaded.
        Requires the argument `thread_id`
        """
        validate(args, ["thread_id"])
        thread = db.thread_get(database, args["thread_id"])
        cherrypy.thread_data.usermap = \
            create_usermap(database, thread["messages"])
        return thread


    @api_method
    def edit_post(self, args, database, user, **kwargs):
        """
        Replace a post with a new body. Requires the arguments
        `thread_id`, `post_id`, and `body`. This method verifies
        that the user can edit a post before commiting the change,
        otherwise an error object is returned whose description
        should be shown to the user.

        To perform sanity checks and retrieve the unformatted body
        of a post without actually attempting to replace it, use
        `edit_query` first.

        Returns the new message object.
        """
        if user == db.anon:
            raise BBJUserError("Anons cannot edit messages.")
        validate(args, ["body", "thread_id", "post_id"])
        return message_edit_commit(
            database, user["user_id"], args["thread_id"], args["post_id"], args["body"])


    @api_method
    def edit_query(self, args, database, user, **kwargs):
        """
        Queries the database to ensure the user can edit a given
        message. Requires the arguments `thread_id` and `post_id`
        (does not require a new body)

        Returns the original message object without any formatting
        on success.
        """
        if user == db.anon:
            raise BBJUserError("Anons cannot edit messages.")
        validate(args, ["thread_id", "post_id"])
        return message_edit_query(
            database, user["user_id"], args["thread_id"], args["post_id"])


    def test(self, **kwargs):
        print(cherrypy.request.body.read())
        return "{\"wow\": \"jolly good show!\"}"
    test.exposed = True



def run():
    cherrypy.quickstart(API(), "/api")


if __name__ == "__main__":
    print("yo lets do that -i shit mang")