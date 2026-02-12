from django.urls import path
from . import views
from .views import email_logs_view

urlpatterns = [
    path("", views.admin_dashboard, name="admin-dashboard"),
    path("subscriptions/", views.subscriptions_view, name="admin-subscriptions"),
    path("orders/", views.orders_view, name="admin-orders"),
    path("create-user/", views.create_user_view, name="admin-create-user"),
    path("users/", views.users_list, name="admin-users"),
    path('logout/', views.logout_user, name='logout'),
    path("subscriptions/create/", views.create_subscription_view, name="admin-create-subscription"),
    path("email-logs/", email_logs_view, name="admin_email_logs"),
    path(
        "orders/<str:encoded_id>/",
        views.order_detail_view,
        name="admin-order-detail"
    ),

    path(
            "subscriptions/edit/<str:encoded_id>/",
            views.edit_subscription_view,
            name="admin-edit-subscription"
        ),

        path(
            "subscriptions/delete/<str:encoded_id>/",
            views.delete_subscription_view,
            name="admin-delete-subscription"
        ),


]
