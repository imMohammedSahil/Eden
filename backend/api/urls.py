from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import AnalysisJobViewSet, UploadView, HealthCheckView

router = DefaultRouter()
router.register(r'jobs', AnalysisJobViewSet)

urlpatterns = [
    path('', include(router.urls)),
    path('health/', HealthCheckView.as_view(), name='health_check'),
    path('upload/', UploadView.as_view()),
]
