from django.urls import path

from OAuth.frienden import FriendenBindingCompleteView, FriendenBindingView
from OAuth.views import OAuthView

urlpatterns = [
    path('qtb/callback', OAuthView.as_view()),
    path('frienden/binding', FriendenBindingView.as_view()),
    path('frienden/complete', FriendenBindingCompleteView.as_view()),
]
