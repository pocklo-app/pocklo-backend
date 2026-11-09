""" Adel Liu 180111

api子路由
"""
from django.urls import path, include

from Resource.picker import PickerFiles, PickerSession, PickerShare
from Resource.workspace import ShareCollectionView, ShareView

urlpatterns = [
    path('picker/session', PickerSession.as_view()),
    path('picker/files', PickerFiles.as_view()),
    path('picker/share', PickerShare.as_view()),
    path('shares/', ShareCollectionView.as_view()),
    path('shares/<uuid:token>', ShareView.as_view()),
    path('user/', include('User.urls')),
    path('res/', include('Resource.urls')),
    path('base/', include('Base.urls')),
    path('oauth/', include('OAuth.urls')),
]
