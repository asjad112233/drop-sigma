from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required


def admin_login_page(request):
    if request.user.is_authenticated and request.user.is_staff:
        return redirect("/")

    error = None
    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        user = authenticate(request, username=username, password=password)
        if user and user.is_staff:
            login(request, user)
            return redirect(request.GET.get("next", "/"))
        elif user and not user.is_staff:
            error = "You don't have admin access."
        else:
            error = "Invalid username or password."

    return render(request, "admin_login.html", {"error": error})


def admin_logout_view(request):
    logout(request)
    return redirect("/login/")


@login_required(login_url="/login/")
def dashboard_page(request):
    if not request.user.is_staff:
        return redirect("/employee/login/")
    return render(request, "dashboard.html")
