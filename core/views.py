from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.contrib.auth.models import User


def setup_admin(request):
    """One-time admin setup endpoint — remove after use."""
    user, created = User.objects.get_or_create(username="admin")
    user.email = "admin@baghawat.com"
    user.is_staff = True
    user.is_superuser = True
    user.set_password("Admin@1234!")
    user.save()
    return JsonResponse({"success": True, "created": created, "msg": "Admin ready. Username: admin, Password: Admin@1234!"})


def homepage(request):
    if request.user.is_authenticated and request.user.is_staff:
        return redirect("/dashboard/")
    return render(request, "home.html")


def admin_login_page(request):
    if request.user.is_authenticated and request.user.is_staff:
        return redirect("/dashboard/")

    # Tab from URL param (vendor / team / admin)
    tab = request.GET.get("tab", "admin")
    error = request.GET.get("error", None)

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        user = authenticate(request, username=username, password=password)
        if user and user.is_staff:
            login(request, user)
            return redirect(request.GET.get("next", "/dashboard/"))
        elif user and not user.is_staff:
            error = "You don't have admin access."
        else:
            error = "Invalid username or password."
        tab = "admin"

    return render(request, "admin_login.html", {"error": error, "tab": tab})


def admin_logout_view(request):
    logout(request)
    return redirect("/")


@login_required(login_url="/login/")
def dashboard_page(request):
    if not request.user.is_staff:
        return redirect("/login/?tab=team")
    return render(request, "dashboard.html")
