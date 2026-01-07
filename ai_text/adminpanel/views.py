from django.contrib.auth.models import User, Group
from django.contrib import messages
from django.shortcuts import redirect
from django.db import IntegrityError
from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.contrib.auth import authenticate, login, logout
from django.http import HttpResponseForbidden
from dashboard.utils.roles import is_admin
from django.core.mail import send_mail
from django.conf import settings
from django.core.paginator import Paginator
from django.db.models import Q
from .models import Subscription
from dashboard.utils.id_encoder import encode_id, decode_id
from dashboard.models import Payment
from django.shortcuts import get_object_or_404
from django.db.models import Sum
import re

# from dashboard.models import UserSubscription

@login_required
def order_detail_view(request, encoded_id):
    if not is_admin(request.user):
        return HttpResponseForbidden()

    order_id = decode_id(encoded_id)

    order = get_object_or_404(
        Payment.objects.select_related("user", "subscription"),
        pk=order_id
    )

    #  Fetch full payment history of this user
    payment_history = (
        Payment.objects
        .filter(user=order.user)
        .select_related("subscription")
        .order_by("-created_at")
    )

    return render(
        request,
        "adminpanel/order_detail.html",
        {
            "order": order,
            "payment_history": payment_history,
        }
    )

def logout_user(request):
    logout(request)
    redirect_url = request.build_absolute_uri("/")
    return redirect(redirect_url)

@login_required
def users_list(request):
    if not is_admin(request.user):
        return HttpResponseForbidden("Access denied")

    search_query = request.GET.get("q", "").strip()

    users_qs = User.objects.all().order_by("-date_joined")

    if search_query:
        users_qs = users_qs.filter(
            Q(username__icontains=search_query) |
            Q(email__icontains=search_query)
        )

    paginator = Paginator(users_qs, 10)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    return render(
        request,
        "adminpanel/users.html",
        {
            "page_obj": page_obj,
            "search_query": search_query,
        }
    )


@login_required
def create_user_view(request):
    if not (request.user.is_superuser or request.user.groups.filter(name="admin").exists()):
        return HttpResponseForbidden("You are not allowed to access this page")

    if request.method == "POST":
        username = request.POST["username"].strip()
        email = request.POST["email"].strip()
        password = request.POST["password"]
        confirm = request.POST["confirm_password"]
        role = request.POST["role"]

        #  password match
        if password != confirm:
            messages.error(request, "Passwords do not match")
            return redirect("admin-create-user")

        #  strong password check
        strong_regex = re.compile(
            r'^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[@$!%*?&]).{8,}$'
        )
        if not strong_regex.match(password):
            messages.error(
                request,
                "Password must be 8+ chars with uppercase, lowercase, number & symbol.",
                extra_tags="user"
            )
            return redirect("admin-create-user")

        # username exists check
        if User.objects.filter(username=username).exists():
            messages.error(request, "Username already exists")
            return redirect("admin-create-user")

        # email exists check (recommended)
        if email and User.objects.filter(email=email).exists():
            messages.error(request, "Email already exists", extra_tags="user")
            return redirect("admin-create-user")

        #  role security
        if role == "superadmin" and not request.user.is_superuser:
            return HttpResponseForbidden()

        try:
            user = User.objects.create_user(
                username=username,
                email=email,
                password=password
            )
        except IntegrityError:
            messages.error(request, "User already exists", extra_tags="user")
            return redirect("admin-create-user")

        # assign role
        if role == "superadmin":
            user.is_superuser = True
            user.is_staff = True
        elif role == "admin":
            admin_group, _ = Group.objects.get_or_create(name="admin")
            user.groups.add(admin_group)
            user.is_staff = True

        user.save()

        #  send email
        send_mail(
            subject="Your Admin Account Credentials",
            message=f"""
                    Hello {username},

                    Your account has been created.

                    Login URL: https://ai.effectivepresentations.com/dashboard/login/
                    Username: {username}

                    Please change your password after login.
                    """,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email],
        )

        messages.success(
            request,
            "User created successfully",
            extra_tags="user"
        )
        return redirect("admin-users")

    return render(request, "adminpanel/create_user.html")

@login_required
def admin_dashboard(request):
    if not is_admin(request.user):
        return HttpResponseForbidden()

    successful_payments = Payment.objects.filter(status="success")

    total_orders = successful_payments.count()

    total_revenue = (
        successful_payments.aggregate(total=Sum("amount"))["total"] or 0
    )

    users_count = User.objects.count()

    context = {
        "revenue": total_revenue,
        "orders": total_orders,
        "users": users_count,
        "is_superadmin": request.user.is_superuser,
        "is_admin": request.user.groups.filter(name="admin").exists(),
    }

    return render(request, "adminpanel/dashboard.html", context)

@login_required
def subscriptions_view(request):
    if not is_admin(request.user):
        return HttpResponseForbidden()

    search = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")
    billing_type = request.GET.get("billing_type", "")

    subscriptions_qs = Subscription.objects.all().order_by("-created_at")

    # Search by name
    if search:
        subscriptions_qs = subscriptions_qs.filter(name__icontains=search)

    # Filter by status
    if status:
        subscriptions_qs = subscriptions_qs.filter(status=status)

    # Filter by billing type
    if billing_type:
        subscriptions_qs = subscriptions_qs.filter(billing_type=billing_type)

    # Pagination
    paginator = Paginator(subscriptions_qs, 10)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    return render(
        request,
        "adminpanel/subscriptions.html",
        {
            "page_obj": page_obj,
            "search": search,
            "status": status,
            "billing_type": billing_type,
        }
    )


@login_required
def create_subscription_view(request):
    if not is_admin(request.user):
        return HttpResponseForbidden()

    if request.method == "POST":
        Subscription.objects.create(
            name=request.POST["name"],
            description=request.POST.get("description", ""),
            price=request.POST["price"],
            billing_type=request.POST["billing_type"],
            status=request.POST["status"]
        )
        messages.success(
            request,
            "Subscription created successfully",
            extra_tags="subscription"
        )
        return redirect("admin-subscriptions")

    return render(request, "adminpanel/create_subscription.html")

@login_required
def orders_view(request):
    if not is_admin(request.user):
        return HttpResponseForbidden()

    search = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")
    page = request.GET.get("page")

    orders_qs = Payment.objects.select_related(
        "user", "subscription"
    ).order_by("-created_at")

    #  Search (username, email, transaction id)
    if search:
        orders_qs = orders_qs.filter(
            Q(transaction_id__icontains=search) |
            Q(user__username__icontains=search) |
            Q(user__email__icontains=search)
        )

    # Filter by status
    if status:
        orders_qs = orders_qs.filter(status=status)

    paginator = Paginator(orders_qs, 10)
    page_obj = paginator.get_page(page)

    return render(
        request,
        "adminpanel/orders.html",
        {
            "page_obj": page_obj,
            "search": search,
            "status": status,
        }
    )


@login_required
def users_view(request):
    if not is_admin(request.user):
        return HttpResponseForbidden()

    return render(request, "adminpanel/users.html")

@login_required
def edit_subscription_view(request, encoded_id):
    if not is_admin(request.user):
        return HttpResponseForbidden()

    pk = decode_id(encoded_id)
    subscription = Subscription.objects.get(pk=pk)

    if request.method == "POST":
        subscription.name = request.POST["name"]
        subscription.description = request.POST.get("description", "")
        subscription.price = request.POST["price"]
        subscription.billing_type = request.POST["billing_type"]
        subscription.status = request.POST["status"]
        subscription.save()

        messages.success(
            request,
            "Subscription Updated successfully",
            extra_tags="subscription"
        )
        return redirect("admin-subscriptions")

    return render(
        request,
        "adminpanel/edit_subscription.html",
        {"subscription": subscription}
    )

@login_required
def delete_subscription_view(request, encoded_id):
    if not is_admin(request.user):
        return HttpResponseForbidden()

    pk = decode_id(encoded_id)

    if request.method == "POST":
        Subscription.objects.filter(pk=pk).delete()
        messages.success(
            request,
            "Subscription deleted successfully",
            extra_tags="subscription"
        )

    return redirect("admin-subscriptions")


