from django.contrib import admin
from django.urls import path
from dashboard import views  # Import your views directly


urlpatterns = [
    # path('', views.speech_tx, name='speech_tx'),
    # path('', views.speech_tx, name='dashboard'),
    path('', views.dashboard_redirect, name='dashboard'),
    path('practice/', views.speech_tx, name='practice'),
    path('recording/', views.recording_view, name='recording'),
    path('login/', views.login_user, name='login'),
    path('register/', views.register_user, name='register'),
    path('logout/', views.logout_user, name='logout'),
    path('recording/<int:rec_id>/', views.recording_detail, name='recording_detail'),
    path('recording/delete/', views.delete_recordings, name='delete_recordings'),
   # path('voice-to-text/', views.voice_to_text, name='voice_to_text'),
    # path('product-view/<int:Pid>', views.productView, name='productView'),
    # path('tracking/', views.tracking, name='tracking'),
    # path('checkout/', views.checkout, name='checkout'),
    # path('contant-us/', views.contact, name='contant'),
    # path('about-us/', views.about, name='about'),
    # path('thank-you', views.thank_you, name='thank_you'),
    # path('checkout_ajax/', views.checkout_ajax, name='checkout_ajax'),
    # path('apply_coupon/', views.apply_coupon, name='apply_coupon'),
    # path('search/', views.pro_search, name='pro_search'),
]
