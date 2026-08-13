from django.urls import path

from demo import views

urlpatterns = [
    path("", views.index, name="index"),
    path("users/<int:user_id>/", views.get_user, name="get_user"),
    path("search/", views.search, name="search"),
    path("slow/", views.slow, name="slow"),
    path("boom/", views.boom, name="boom"),
    path("health/", views.health, name="health"),
]
