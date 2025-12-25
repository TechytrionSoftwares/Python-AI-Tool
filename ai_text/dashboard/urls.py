from django.contrib import admin
from django.urls import path
from dashboard import views  # Import your views directly
from django.contrib.auth import views as auth_views


urlpatterns = [
    path('', views.dashboard_redirect, name='dashboard'),
    path('practice/', views.speech_tx, name='practice'),
    path('recording/', views.recording_view, name='recording'),
    path('settings/', views.settings_view, name='settings'),
    path('login/', views.login_user, name='login'),
    path('register/', views.register_user, name='register'),
    path('logout/', views.logout_user, name='logout'),
    path('recording/<int:rec_id>/', views.recording_detail, name='recording_detail'),
    path('recording/delete/', views.delete_recordings, name='delete_recordings'),
    path("change-password/", views.change_password_ajax, name="change_password_ajax"),
    path("recording-status/<int:recording_id>/", views.recording_status, name="recording_status"),
    # path("upload-files/", views.upload_multiple_files, name="upload_multiple_files"),
    # path("start-conversion/", views.start_conversion, name="start_conversion"),


]
