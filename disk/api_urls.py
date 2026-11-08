""" Adel Liu 180111

api子路由
"""
from django.urls import path, include

from Resource.workspace import ShareCollectionView, ShareView

urlpatterns = [
    path('shares/', ShareCollectionView.as_view()),
    path('shares/<uuid:token>', ShareView.as_view()),
    path('user/', include('User.urls')),
    path('res/', include('Resource.urls')),
    path('base/', include('Base.urls')),
    path('oauth/', include('OAuth.urls')),
]
