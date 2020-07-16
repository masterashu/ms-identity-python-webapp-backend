import uuid
import requests
from flask import Flask, render_template, session, request, redirect, url_for
from flask_session import Session  # https://pythonhosted.org/Flask-Session
import msal
import app_config
import pyodbc
import struct
import adal
from msrestazure.azure_active_directory import AADTokenCredentials

app = Flask(__name__)
app.config.from_object(app_config)
Session(app)

# This section is needed for url_for("foo", _external=True) to automatically
# generate http scheme when this sample is running on localhost,
# and to generate https scheme when it is deployed behind reversed proxy.
# See also https://flask.palletsprojects.com/en/1.0.x/deploying/wsgi-standalone/#proxy-setups
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

@app.route("/")
def index():
    if not session.get("user"):
        return redirect(url_for("login"))
    return render_template('index.html', user=session["user"], version=msal.__version__)

@app.route("/login")
def login():
    session["state"] = str(uuid.uuid4())

    # Technically we could use empty list [] as scopes to do just sign in,
    # here we choose to also collect end user consent upfront
    auth_url = _build_auth_url(scopes=app_config.DELEGATED_PERMISSONS, state=session["state"])
    return render_template("login.html", auth_url=auth_url, version=msal.__version__)

@app.route(app_config.REDIRECT_PATH)  # Its absolute URL must match your app's redirect_uri set in AAD
def authorized():
    if request.args.get('state') != session.get("state"):
        return redirect(url_for("index"))  # No-OP. Goes back to Index page
    if "error" in request.args:  # Authentication/Authorization failure
        return render_template("auth_error.html", result=request.args)
    if request.args.get('code'):
        cache = _load_cache()
        result = _build_msal_app(cache=cache).acquire_token_by_authorization_code(
            request.args['code'],
            scopes=app_config.DELEGATED_PERMISSONS,  # Misspelled scope would cause an HTTP 400 error here
            redirect_uri=url_for("authorized", _external=True))
        if "error" in result:
            return render_template("auth_error.html", result=result)
        session["user"] = result.get("id_token_claims")
        _save_cache(cache)
    return redirect(url_for("index"))

@app.route("/logout")
def logout():
    session.clear()  # Wipe out user and its token cache from session
    return redirect(  # Also logout from your tenant's web session
        app_config.AUTHORITY + "/oauth2/v2.0/logout" +
        "?post_logout_redirect_uri=" + url_for("index", _external=True))

@app.route("/getcustomerdata")
def get_customer_data():
    return _get_data("premium_user_access", "SalesLT.Customer")

@app.route("/getproductdata")
def get_product_data():
    return _get_data("basic_user_access", "SalesLT.Product")

@app.route("/getproductdatadaemon")
def get_product_data_daemon():

    app2 = msal.ConfidentialClientApplication(
        app_config.CLIENT_ID, authority=app_config.AUTHORITY,
        client_credential=app_config.CLIENT_SECRET
    )
    result = app2.acquire_token_for_client(scopes=app_config.APPLICATION_PERMISSIONS)
    token = result['access_token']

    if app_config.BACKEND_SETTINGS.get("Type") == "Database":
        row = _retrieve_data_from_database(token, "SalesLT.Product")
    else:
        row = _retrieve_data_from_function(token,None)

    return render_template('display.html', result={'message': str(row)})

@app.route("/graphcall")
def graphcall():
    token = _get_token_from_cache(app_config.DELEGATED_PERMISSONS)
    if not token:
        return redirect(url_for("login"))

    graph_data = requests.get(  # Use token to call downstream service
        app_config.GRAPH_ENDPOINT,
        headers={'Authorization': 'Bearer ' + token['access_token']},
        ).json()
    return render_template('display.html', result=graph_data)

def _load_cache():
    cache = msal.SerializableTokenCache()
    if session.get("token_cache"):
        cache.deserialize(session["token_cache"])
    return cache

def _save_cache(cache):
    if cache.has_state_changed:
        session["token_cache"] = cache.serialize()

def _build_msal_app(cache=None, authority=None):
    return msal.ConfidentialClientApplication(
        app_config.CLIENT_ID, authority=authority or app_config.AUTHORITY,
        client_credential=app_config.CLIENT_SECRET, token_cache=cache)

def _build_auth_url(authority=None, scopes=None, state=None):
    return _build_msal_app(authority=authority).get_authorization_request_url(
        scopes or [],
        state=state or str(uuid.uuid4()),
        redirect_uri=url_for("authorized", _external=True))

def _get_token_from_cache(scope):
    cache = _load_cache()  # This web app maintains one cache per session
    cca = _build_msal_app(cache=cache)
    accounts = cca.get_accounts()
    if accounts:  # So all account(s) belong to the current signed-in user
        result = cca.acquire_token_silent(scope, account=accounts[0])
        _save_cache(cache)
        return result

app.jinja_env.globals.update(_build_auth_url=_build_auth_url)  # Used in template

#
# New code compared to original ms-identity-python-webapp to access the database
#
def _get_data(role_required, table):

    token = _get_token_from_cache(app_config.DELEGATED_PERMISSONS)
    if not token:
        return redirect(url_for("login"))

    if app_config.AAD_ROLE_CHECK and role_required != "basic_user_access":
        # check if claims in bearer token of user allows to retrieve data
        if _check_user_has_role_in_token(role_required) == False:
            error_message = "role " + role_required + " not present in id token of user"
            return render_template('display.html', result={'message': "'" + error_message + "'"})

    if app_config.DELEGATED_PERMISSONS[0] == "User.Read":
        # MI of app registration is needed to authenticate to backend
        token = _create_token_from_app_registration()
    else:
        # use bearer token of user for AAD passthrough
        token = token['access_token']

    # Retrieve data from database and return it
    if app_config.BACKEND_SETTINGS.get("Type") == "Database":
        row = _retrieve_data_from_database(token, table)
    else:
        row = _retrieve_data_from_function(token, table)
    return render_template('display.html', result={'message': "'" + str(row) + "'"})

def _check_user_has_role_in_token(role):
    # Check if required user role is present as claim in the ID token of the user
    if not "roles" in session["user"]:
        return False

    user_roles = session["user"]["roles"]
    for user_role in user_roles:
        print (user_role)
        if user_role == role:
            return True
    return False

def _create_token_from_app_registration():
    # Authenticate using service principal w/ key.
    app3 = msal.ConfidentialClientApplication(
        app_config.CLIENT_ID, authority=app_config.AUTHORITY,
        client_credential=app_config.CLIENT_SECRET
    )

    result = app3.acquire_token_for_client(scopes=app_config.APPLICATION_PERMISSIONS)
    return result['access_token']

def _retrieve_data_from_function(token, table):

    url_function= app_config.BACKEND_SETTINGS.get("Connection").get("URL") + "&name=" + str(table)
    response = requests.get(url_function, headers={'Authorization': "Bearer " + token})

    status = str(response.status_code)
    print("token: " + token +  "status: " + status)

    return status + str(response.content).replace("\u0027", "")

def _retrieve_data_from_database(token, table):
    
    accessToken = bytes(token, 'utf-8')
    exptoken = b""
    for i in accessToken:
        exptoken += bytes({i})
        exptoken += bytes(1)
    tokenstruct = struct.pack("=i", len(exptoken)) + exptoken

    server  = app_config.BACKEND_SETTINGS.get("Connection").get("SQL_SERVER")
    database = app_config.BACKEND_SETTINGS.get("Connection").get("DATABASE")
    connstr = 'DRIVER={ODBC Driver 17 for SQL Server};SERVER='+server+';DATABASE='+database
    #tokenstruct = struct.pack("=i", len(exptoken)) + exptoken
    conn = pyodbc.connect(connstr, attrs_before = { 1256:tokenstruct })
    
    cursor = conn.cursor()
    cursor.execute("SELECT top 10 * FROM " + str(table))
    row = cursor.fetchall()
    return row

if __name__ == "__main__":
    app.run()