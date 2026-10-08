import os
import dash
from dash import dcc, html, Input, Output
import flask
from flask import session, request, redirect
from msal import ConfidentialClientApplication
from dotenv import load_dotenv

# 1. LOAD CONFIGURATION
load_dotenv()
TENANT_ID = os.environ.get("TENANT_ID")
CLIENT_ID = os.environ.get("CLIENT_ID")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET")
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
REDIRECT_PATH = "/getAToken"

# Parse allowed security groups from comma-separated string
ALLOWED_GROUPS_ENV = os.environ.get("ALLOWED_GROUPS", "")
ALLOWED_GROUPS = [g.strip() for g in ALLOWED_GROUPS_ENV.split(",") if g.strip()]

# 2. DEFINE SCOPES
# APP_SCOPE is used for the initial frontend authentication
APP_SCOPE = [f"{CLIENT_ID}/.default"]
# FABRIC_SCOPE is used for the OBO exchange (App -> Fabric)
FABRIC_SCOPE = ["https://analysis.windows.net/powerbi/api/.default"]

# 3. INITIALIZE FLASK & MSAL
server = flask.Flask(__name__)
server.secret_key = os.urandom(24)

def _build_msal_app():
    return ConfidentialClientApplication(
        client_id=CLIENT_ID,
        client_credential=CLIENT_SECRET,
        authority=AUTHORITY
    )

# ---------------------------------------------------------
# FLASK ROUTES (AUTHENTICATION & OBO LOGIC)
# ---------------------------------------------------------

@server.route("/login")
def login():
    session["state"] = os.urandom(16).hex()
    msal_app = _build_msal_app()
    auth_url = msal_app.get_authorization_request_url(
        scopes=APP_SCOPE,
        state=session["state"],
        redirect_uri=flask.request.host_url.rstrip("/") + REDIRECT_PATH
    )
    return redirect(auth_url)

@server.route(REDIRECT_PATH)
def authorized():
    # Verify state to prevent CSRF attacks
    if request.args.get("state") != session.get("state"):
        return "State mismatch error", 400
    
    if "error" in request.args:
        return f"Auth Error: {request.args.get('error_description')}"
    
    if request.args.get("code"):
        msal_app = _build_msal_app()
        
        # Step 1: Exchange Auth Code for App Token (Frontend Token)
        app_result = msal_app.acquire_token_by_authorization_code(
            request.args["code"],
            scopes=APP_SCOPE,
            redirect_uri=flask.request.host_url.rstrip("/") + REDIRECT_PATH
        )
        
        if "access_token" in app_result:
            id_token_claims = app_result.get("id_token_claims", {})
            user_groups = id_token_claims.get("groups", [])
            
            # --- SECURITY GROUP CHECK (App-Level Authorization) ---
            if ALLOWED_GROUPS and not any(group in user_groups for group in ALLOWED_GROUPS):
                user_name = id_token_claims.get('name', 'User')
                print(f"⚠️ Unauthorized attempt by {user_name}. Not in allowed security groups.")
                session.clear()
                return f"""
                <html>
                <body style="font-family: 'Segoe UI', sans-serif; padding: 40px; text-align: center; background-color: #f8f9fa;">
                    <h2 style="color: #d13438;">Access Denied</h2>
                    <p>Your account does not have permission to access this application (Security Group restriction).</p>
                    <a href='/login' style="padding: 10px 20px; background-color: #0078D4; color: white; text-decoration: none; border-radius: 4px; display: inline-block; margin-top: 20px;">Try another account</a>
                </body>
                </html>
                """, 403
            # ------------------------------------------------------
            
            app_token = app_result["access_token"]
            session["frontend_token_acquired"] = True
            
            # Step 2: The OBO Exchange (Trade App Token for Fabric Token)
            obo_result = msal_app.acquire_token_on_behalf_of(
                user_assertion=app_token,
                scopes=FABRIC_SCOPE
            )
            
            if "access_token" in obo_result:
                session["fabric_token_acquired"] = True
            else:
                session["fabric_token_error"] = obo_result.get("error_description", "Unknown OBO error")
                
    return redirect("/")

@server.route("/logout")
def logout():
    session.clear()
    return redirect("/")

# ---------------------------------------------------------
# DASH UI (THE PRESENTATION LAYER)
# ---------------------------------------------------------

app = dash.Dash(__name__, server=server, url_base_pathname="/")
app.title = "OBO Flow Demo"

app.layout = html.Div(
    style={"fontFamily": "Segoe UI, sans-serif", "maxWidth": "800px", "margin": "40px auto", "padding": "20px"},
    children=[
        html.H1("Entra ID OBO Flow for Microsoft Fabric", style={"color": "#0078D4"}),
        html.P("This application demonstrates securely exchanging a frontend MSAL token for a backend Fabric token, protected by Security Groups."),
        
        html.Div(id="status-container", style={"margin": "30px 0"}),
        
        dcc.Location(id="url", refresh=False),
        html.Div(id="auth-buttons")
    ]
)

@app.callback(
    [Output("status-container", "children"),
     Output("auth-buttons", "children")],
    [Input("url", "pathname")]
)
def update_ui(pathname):
    has_frontend = session.get("frontend_token_acquired", False)
    has_fabric = session.get("fabric_token_acquired", False)
    obo_error = session.get("fabric_token_error")

    if not has_frontend:
        status = html.Div(
            "🔒 Status: Not Authenticated", 
            style={"padding": "15px", "backgroundColor": "#f3f2f1", "borderLeft": "5px solid #8a8886"}
        )
        buttons = html.A(
            "Login with Microsoft", 
            href="/login", 
            style={"display": "inline-block", "padding": "10px 20px", "backgroundColor": "#0078D4", "color": "white", "textDecoration": "none", "borderRadius": "4px"}
        )
    else:
        status_items = [
            html.Div("✅ Frontend App Token acquired successfully.", style={"color": "#107c10", "marginBottom": "10px"})
        ]
        if has_fabric:
            status_items.append(html.Div("✅ OBO Exchange successful: Fabric Token acquired!", style={"color": "#107c10", "fontWeight": "bold"}))
        elif obo_error:
            status_items.append(html.Div(f"❌ OBO Exchange failed: {obo_error}", style={"color": "#d13438"}))
            
        status = html.Div(
            status_items,
            style={"padding": "15px", "backgroundColor": "#dff6dd", "borderLeft": "5px solid #107c10"}
        )
        buttons = html.A(
            "Logout", 
            href="/logout", 
            style={"display": "inline-block", "padding": "10px 20px", "backgroundColor": "#d13438", "color": "white", "textDecoration": "none", "borderRadius": "4px"}
        )

    return status, buttons

if __name__ == "__main__":
    # Note: Port 8000 matches the redirect URI in the README
    app.run_server(debug=True, port=8000)