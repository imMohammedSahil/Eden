import os
from django.conf import settings
from rest_framework import serializers
from core_app.models import AnalysisJob, AnalysisReport, ClaimRecord, MediaAsset

class ClaimRecordSerializer(serializers.ModelSerializer):
    # Aliases for frontend compatibility
    reasoning = serializers.CharField(source='contextual_reasoning')
    confidence = serializers.FloatField(source='confidence_score')
    verdict = serializers.CharField(source='classification_label')
    text = serializers.SerializerMethodField()
    claim = serializers.SerializerMethodField()
    
    class Meta:
        model = ClaimRecord
        fields = [
            'id', 'claim_text', 'detection_source', 'classification_label', 
            'confidence_score', 'contextual_reasoning', 'transcript_reference', 
            'ocr_reference', 'related_sources', 'created_at',
            # Aliases
            'reasoning', 'confidence', 'verdict', 'text', 'claim'
        ]

    def get_text(self, obj):
        return obj.claim_text

    def get_claim(self, obj):
        return obj.claim_text

class MediaAssetSerializer(serializers.ModelSerializer):
    file_url = serializers.SerializerMethodField()

    class Meta:
        model = MediaAsset
        fields = ['id', 'asset_type', 'metadata', 'file_url', 'created_at']

    def get_file_url(self, obj):
        if not obj.file_path:
            return None
        
        # Convert absolute filesystem path to a URL path relative to MEDIA_URL
        try:
            # On Windows, path comparison can be tricky with slashes/case
            media_root_abs = os.path.abspath(settings.MEDIA_ROOT)
            file_path_abs = os.path.abspath(obj.file_path)
            
            if file_path_abs.startswith(media_root_abs):
                rel_path = os.path.relpath(file_path_abs, media_root_abs)
                # Normalize slashes for URL
                url_path = rel_path.replace('\\', '/')
                request = self.context.get('request')
                if request:
                    return request.build_absolute_uri(settings.MEDIA_URL + url_path)
                return settings.MEDIA_URL + url_path
        except Exception:
            pass
        return None

class AnalysisReportSerializer(serializers.ModelSerializer):
    class Meta:
        model = AnalysisReport
        fields = ['id', 'report_data', 'created_at']

class AnalysisJobSerializer(serializers.ModelSerializer):
    report = AnalysisReportSerializer(read_only=True)
    claims = ClaimRecordSerializer(many=True, read_only=True)
    media_assets = MediaAssetSerializer(many=True, read_only=True)

    class Meta:
        model = AnalysisJob
        fields = [
            'id', 'instagram_url', 'ingestion_source', 'original_filename',
            'analysis_type', 'status', 'processing_phase', 'error_message',
            'created_at', 'updated_at', 'report', 'claims', 'media_assets',
        ]
        read_only_fields = ['status', 'processing_phase', 'error_message', 'report', 'claims', 'media_assets']

class CreateAnalysisJobSerializer(serializers.Serializer):
    instagram_url = serializers.URLField()
    analysis_mode = serializers.ChoiceField(choices=['text', 'audio'], required=False, default='text')

    def validate_instagram_url(self, value):
        from urllib.parse import urlparse
        parsed = urlparse(value)
        hostname = (parsed.netloc or '').lower()
        valid_domains = ('instagram.com', 'www.instagram.com', 'instagr.am', 'www.instagr.am')
        if not any(hostname == d or hostname.endswith('.' + d) for d in ('instagram.com', 'instagr.am')):
            raise serializers.ValidationError("URL must be a valid Instagram link (e.g., https://www.instagram.com/reel/...).")
        
        path = parsed.path.strip('/')
        if not path:
            raise serializers.ValidationError("Instagram URL must point to a specific post, reel, or video.")
        return value
