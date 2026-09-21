from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from celery import chain
from django.conf import settings
from pathlib import Path
import os

from core_app.models import AnalysisJob, AnalysisReport, ClaimRecord, MediaAsset, MediaAssetType
from .serializers import AnalysisJobSerializer, CreateAnalysisJobSerializer, AnalysisReportSerializer, ClaimRecordSerializer
from ingestion.tasks import ingest_instagram_media
from processing.tasks import extract_ocr_text, extract_audio_transcription
from analysis.tasks import analyze_job_content


import threading
import time
from django.db import close_old_connections
from core_app.models import AnalysisJobStatus

class HealthCheckView(APIView):
    """
    Lightweight health check endpoint for UptimeRobot, Render keep-alive,
    and frontend pre-warming. Returns 200 OK instantly.
    """
    authentication_classes = []
    permission_classes = []

    def get(self, request, *args, **kwargs):
        return Response({
            "status": "healthy",
            "service": "eden-backend",
            "timestamp": time.time()
        }, status=status.HTTP_200_OK)

class DummyTask:
    def __init__(self):
        class Request:
            retries = 0
        self.request = Request()
        self.max_retries = 3
    def retry(self, exc=None, countdown=0):
        time.sleep(countdown)
        raise exc

def get_task_func(task):
    if hasattr(task, '__wrapped__'):
        wrapped = task.__wrapped__
        if hasattr(wrapped, '__func__'):
            return wrapped.__func__
        return wrapped
    if hasattr(task, 'undecorated'):
        return task.undecorated
    return task

def run_pipeline_async(job_id: int, analysis_mode: str):
    def run():
        # Short delay to ensure the request returns 201 Created to the client first
        time.sleep(0.5)
        from ingestion.tasks import ingest_instagram_media
        from processing.tasks import extract_ocr_text, extract_audio_transcription
        from analysis.tasks import analyze_job_content
        from core_app.models import AnalysisJob, AnalysisJobStatus
        
        dummy_task = DummyTask()
        try:
            close_old_connections()
            print(f"!!! Async Thread: Ingesting job {job_id}...")
            get_task_func(ingest_instagram_media)(dummy_task, job_id)
            
            job = AnalysisJob.objects.get(id=job_id)
            if job.status == AnalysisJobStatus.FAILED:
                print(f"!!! Async Thread: Ingestion failed for job {job_id}. Aborting pipeline. !!!")
                return
                
            if analysis_mode == 'audio':
                print(f"!!! Async Thread: Transcribing audio for job {job_id}...")
                get_task_func(extract_audio_transcription)(dummy_task, job_id)
            else:
                print(f"!!! Async Thread: Running OCR for job {job_id}...")
                get_task_func(extract_ocr_text)(dummy_task, job_id)
                
            job = AnalysisJob.objects.get(id=job_id)
            if job.status == AnalysisJobStatus.FAILED:
                print(f"!!! Async Thread: Extraction failed for job {job_id}. Aborting pipeline. !!!")
                return
                
            print(f"!!! Async Thread: Analyzing content for job {job_id}...")
            get_task_func(analyze_job_content)(job_id)
            print(f"!!! Async Thread: Pipeline success for job {job_id}!")
        except Exception as e:
            print(f"!!! Async Thread exception for job {job_id}: {e} !!!")
            try:
                job = AnalysisJob.objects.get(id=job_id)
                if job.status != AnalysisJobStatus.FAILED:
                    job.status = AnalysisJobStatus.FAILED
                    job.error_message = f"Async pipeline error: {str(e)}"
                    job.save()
            except Exception:
                pass
        finally:
            close_old_connections()

    thread = threading.Thread(target=run)
    thread.daemon = True
    thread.start()


class AnalysisJobViewSet(viewsets.ModelViewSet):
    queryset = AnalysisJob.objects.all().order_by('-created_at')
    serializer_class = AnalysisJobSerializer

    def create(self, request, *args, **kwargs):
        serializer = CreateAnalysisJobSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        instagram_url  = serializer.validated_data['instagram_url']
        analysis_mode  = serializer.validated_data.get('analysis_mode', 'text')

        mode_mapping   = {'text': 'TEXT', 'audio': 'AUDIO'}
        analysis_type  = mode_mapping.get(analysis_mode, 'TEXT')

        try:
            job = AnalysisJob.objects.create(
                instagram_url=instagram_url,
                analysis_type=analysis_type,
            )
            run_pipeline_async(job.id, analysis_mode)
            return Response(AnalysisJobSerializer(job).data, status=status.HTTP_201_CREATED)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['get'])
    def status(self, request, pk=None):
        job = self.get_object()
        return Response({
            'id': job.id,
            'status': job.status,
            'processing_phase': job.processing_phase,
            'error_message': job.error_message,
        })


# ── Local Upload Endpoint ──────────────────────────────────────────────────────

ALLOWED_EXTENSIONS = {'.mp4', '.mov', '.avi', '.mkv', '.webm'}


class UploadView(APIView):
    """
    POST /api/upload/   multipart/form-data
    Fields:
      file          — video file (.mp4/.mov/.avi/.mkv/.webm)
      analysis_mode — text | audio   (default: text)

    Creates an AnalysisJob with ingestion_source=UPLOAD, saves the file to
    media/{job_id}/source_media{ext}, creates a MediaAsset record, and fires
    the mode-selective processing pipeline chain.
    """

    def post(self, request, *args, **kwargs):
        uploaded = request.FILES.get('file')
        if not uploaded:
            return Response({'error': 'No file provided.'}, status=status.HTTP_400_BAD_REQUEST)

        ext = Path(uploaded.name).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            return Response(
                {'error': f'Unsupported file type {ext!r}. Allowed: {sorted(ALLOWED_EXTENSIONS)}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        max_bytes = getattr(settings, 'UPLOAD_MAX_FILE_SIZE_BYTES', 500 * 1024 * 1024)
        if uploaded.size > max_bytes:
            return Response(
                {'error': f'File too large ({uploaded.size // 1024 // 1024} MB). Max is {max_bytes // 1024 // 1024} MB.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        analysis_mode = request.data.get('analysis_mode', 'text')
        mode_mapping  = {'text': 'TEXT', 'audio': 'AUDIO'}
        analysis_type = mode_mapping.get(analysis_mode, 'TEXT')

        try:
            job = AnalysisJob.objects.create(
                instagram_url=None,
                original_filename=uploaded.name,
                ingestion_source='UPLOAD',
                analysis_type=analysis_type,
            )

            job_dir   = Path(settings.MEDIA_ROOT) / str(job.id)
            job_dir.mkdir(parents=True, exist_ok=True)
            file_name = f'source_media{ext}'
            file_path = job_dir / file_name

            with open(file_path, 'wb') as f:
                for chunk in uploaded.chunks():
                    f.write(chunk)

            MediaAsset.objects.create(
                job=job,
                asset_type=MediaAssetType.VIDEO,
                file_path=str(file_path),
                file_size=file_path.stat().st_size,
                metadata={'page_title': uploaded.name, 'source_url': 'local_upload'},
                processing_status='UPLOADED',
            )

            # launch the thread asynchronously
            run_pipeline_async(job.id, analysis_mode)

            return Response(AnalysisJobSerializer(job).data, status=status.HTTP_201_CREATED)

        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
