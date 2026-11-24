"""Login, signup and logout pages."""
from fastapi import APIRouter, Form, Request

from middleware import auth, rate_limit
from web.common import check_origin, redirect, render, safe_next

router = APIRouter()


@router.get("/login")
def login_page(request: Request, next: str = "/app"):
    if auth.current_user(request):
        return redirect(safe_next(next))
    return render(request, "login.html", {"next": safe_next(next), "error": ""})


@router.post("/login")
def login_submit(request: Request, username: str = Form(..., max_length=64), password: str = Form(..., max_length=200),
                 next: str = Form("/app")):
    check_origin(request)
    if not rate_limit.allow_login(request):
        return render(request, "login.html", {"next": safe_next(next), "error": rate_limit.RATE_LIMITED}, 429)
    user = auth.login(username, password)
    if not user:
        return render(request, "login.html", {"next": safe_next(next), "error": "Wrong username or password."}, 401)
    response = redirect("/admin" if user.get("is_admin") and safe_next(next) == "/app" else safe_next(next))
    auth.set_cookie(response, user["user_id"])
    return response


@router.get("/signup")
def signup_page(request: Request):
    if auth.current_user(request):
        return redirect("/app")
    return render(request, "signup.html", {"error": "", "username": "", "display_name": ""})


@router.post("/signup")
def signup_submit(request: Request, username: str = Form(..., max_length=64), password: str = Form(..., max_length=200),
                  display_name: str = Form("", max_length=80)):
    check_origin(request)
    if not rate_limit.allow_login(request):
        return render(request, "signup.html", {"error": rate_limit.RATE_LIMITED, "username": username,
                                               "display_name": display_name}, 429)
    user, error = auth.signup(username, password, display_name)
    if not user:
        return render(request, "signup.html", {"error": error, "username": username,
                                               "display_name": display_name}, 400)
    response = redirect("/app")
    auth.set_cookie(response, user["user_id"])
    return response


@router.post("/logout")
def logout(request: Request):
    check_origin(request)
    response = redirect("/")
    auth.clear_cookie(response)
    return response
