from django.urls import path

from OAuth import admin, provider
from OAuth.frienden import (FriendenBindingCompleteView, FriendenBindingView,
                            FriendenLoginCompleteView, FriendenLoginView)
from OAuth.views import OAuthView

urlpatterns = [
    path('qtb/callback', OAuthView.as_view()),
    path('frienden/login', FriendenLoginView.as_view()),
    path('frienden/login/complete', FriendenLoginCompleteView.as_view()),
    path('frienden/binding', FriendenBindingView.as_view()),
    path('frienden/complete', FriendenBindingCompleteView.as_view()),
    path('applications/', provider.Applications.as_view()),
    path('applications/<str:client_id>/', provider.ApplicationSettings.as_view()),
    path('admin/email-code/', admin.EmailCode.as_view()),
    path('admin/session/', admin.Session.as_view()),
    path('admin/mfa/', admin.Mfa.as_view()),
    path('admin/applications/', admin.AdminApplications.as_view()),
    path('admin/applications/<str:client_id>/', admin.AdminApplications.as_view()),
    path('authorize/', provider.AuthorizationStart.as_view()),
    path('requests/<uuid:uid>/', provider.Consent.as_view()),
    path('token/', provider.Token.as_view()),
    path('introspect/', provider.Introspect.as_view()),
    path('revoke/', provider.Revoke.as_view()),
    path('grants/', provider.OwnerGrants.as_view()),
    path('grants/<uuid:uid>/', provider.OwnerGrant.as_view()),
    path('files/', provider.Files.as_view()),
    path('files/<str:resource_id>/', provider.Files.as_view()),
    path('files/<str:resource_id>/download/', provider.Download.as_view()),
    path('shares/', provider.Shares.as_view()),
]
