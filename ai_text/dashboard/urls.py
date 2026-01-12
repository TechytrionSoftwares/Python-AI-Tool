from django.contrib import admin
from django.urls import path
from dashboard import views  # Import your views directly
from django.contrib.auth import views as auth_views
from dashboard.utils.authorize import authorize_net_webhook


urlpatterns = [
    path('', views.dashboard_redirect, name='dashboard'),
    path('practice/', views.speech_tx, name='practice'),
    path('recording/', views.recording_view, name='recording'),
    path('settings/', views.settings_view, name='settings'),
    path('login/', views.login_user, name='login'),
    path('register/', views.register_user, name='register'),
    path('logout/', views.logout_user, name='logout'),
    path('recording/delete/', views.delete_recordings, name='delete_recordings'),
    path("recording-status/<int:recording_id>/", views.recording_status, name="recording_status"),
    path('recording/<str:encoded_id>/', views.recording_detail, name='recording_detail'),
    path("change-password/", views.change_password_ajax, name="change_password_ajax"),
    # path("upload-files/", views.upload_multiple_files, name="upload_multiple_files"),
    # path("start-conversion/", views.start_conversion, name="start_conversion"),
    path("checkout/<int:subscription_id>/", views.checkout, name="checkout"),
    path("webhooks/authorize-net/", authorize_net_webhook, name="authorize-net-webhook"),
    path("subscription/cancel/", views.cancel_subscription, name="cancel_subscription"),
    path("subscription/resume/", views.resume_subscription, name="resume_subscription"),
    path('subscription/change-plan/', views.change_subscription_plan, name='change_subscription_plan'),
    path('subscription/cancel-plan-change/', views.cancel_plan_change, name='cancel_plan_change'),
    path('subscription/preview-change/', views.preview_plan_change, name='preview_plan_change'),
]
