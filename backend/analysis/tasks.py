import logging
import urllib.request
import urllib.parse
import re
from celery import shared_task
from django.db import transaction
from core_app.models import AnalysisJob, AnalysisJobStatus, AnalysisReport, ClaimRecord

logger = logging.getLogger(__name__)

def fetch_search_sources(query: str) -> list:
    """
    Queries DuckDuckGo HTML search results for the top 3-4 web references.
    Falls back to Wikipedia API if DuckDuckGo blocks the request with a captcha.
    Returns a list of dictionaries: [{'title': '...', 'url': '...', 'snippet': '...'}]
    """
    if not query or len(query.strip()) < 3:
        return []
    logger.info(f"Executing web search for query: {query}")
    
    links = []
    
    # 1. Try DuckDuckGo HTML Search
    try:
        url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
        req = urllib.request.Request(
            url,
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'
            }
        )
        with urllib.request.urlopen(req, timeout=2) as response:
            html = response.read().decode('utf-8')
            
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, 'html.parser')
            results = soup.find_all('div', class_='result')
            for r in results[:4]:
                title_el = r.find('a', class_='result__url')
                snippet_el = r.find('a', class_='result__snippet')
                if title_el:
                    href = title_el.get('href', '')
                    if href.startswith('//duckduckgo.com/l/?uddg='):
                        parsed = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
                        href = parsed.get('uddg', [href])[0]
                    elif href.startswith('/l/?uddg='):
                        parsed = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
                        href = parsed.get('uddg', [href])[0]
                    
                    title = title_el.get_text(strip=True)
                    snippet = snippet_el.get_text(strip=True) if snippet_el else ""
                    
                    if href and title:
                        links.append({
                            'title': title,
                            'url': href,
                            'snippet': snippet
                        })
        except Exception as e:
            logger.warning(f"BeautifulSoup parsing failed for DDG: {e}")
            
    except Exception as e:
        logger.warning(f"DuckDuckGo search request failed for '{query}': {e}")
        
    # 2. Yahoo Search Fallback (if DDG returned 0 links due to captcha or block)
    if not links:
        logger.info(f"DDG returned 0 results (possible Captcha). Falling back to Yahoo Search for: {query}")
        try:
            from bs4 import BeautifulSoup
            import re
            
            yahoo_url = 'https://search.yahoo.com/search?p=' + urllib.parse.quote(query)
            yahoo_req = urllib.request.Request(
                yahoo_url,
                headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
            )
            
            with urllib.request.urlopen(yahoo_req, timeout=2) as yahoo_response:
                yahoo_html = yahoo_response.read().decode('utf-8')
                yahoo_soup = BeautifulSoup(yahoo_html, 'html.parser')
                
                # Yahoo search results are typically in divs with class 'compTitle'
                results = yahoo_soup.find_all('div', class_='compTitle')
                for r in results[:4]:
                    a_tag = r.find('a')
                    if a_tag:
                        raw_url = a_tag.get('href', '')
                        title = a_tag.get_text(strip=True)
                        
                        # Clean up Yahoo redirect URLs
                        clean_url = raw_url
                        if 'RU=' in raw_url:
                            try:
                                clean_url = urllib.parse.unquote(raw_url.split('RU=')[1].split('/')[0])
                            except Exception:
                                pass
                                
                        if clean_url and title:
                            links.append({
                                'title': title,
                                'url': clean_url,
                                'snippet': "Source fetched via Yahoo Web Search."
                            })
        except Exception as fallback_e:
            logger.error(f"Yahoo Search fallback failed for '{query}': {fallback_e}")
            
    return links

@shared_task
def analyze_job_content(job_id: int):
    # Initialize ALL modality variables immediately
    ocr_text = ""
    transcript_text = ""
    audio_detected = False
    speech_detected = False

    try:
        job = AnalysisJob.objects.get(id=job_id)
        # ── Pipeline failure guard ────────────────────────────────────────────
        if job.status == AnalysisJobStatus.FAILED:
            return {"status": "skipped", "reason": "upstream_task_failed"}
        # ─────────────────────────────────────────────────────────────────────
        job.status = AnalysisJobStatus.ANALYZING
        job.processing_phase = 'Analyzing extracted content (Multi-Provider)'
        job.save()

        ocr_asset = job.media_assets.filter(asset_type='OCR_RESULTS').first()
        if ocr_asset and ocr_asset.metadata:
            ocr_text = ocr_asset.metadata.get('unified_transcript', '')

        transcript_asset = job.media_assets.filter(asset_type='TRANSCRIPT_RESULTS').first()
        if transcript_asset and transcript_asset.metadata:
            transcript_text = transcript_asset.metadata.get('unified_transcript', '')
            audio_detected = transcript_asset.metadata.get('audio_detected', False)
            speech_detected = transcript_asset.metadata.get('speech_detected', False)

        if not ocr_text and not transcript_text:
            # Graceful empty state, do not fail pipeline
            summary_msg = "No readable text or speech detected in this content."
            if job.analysis_type == 'AUDIO':
                if not audio_detected:
                    summary_msg = "No audio stream was detected in this content."
                elif not speech_detected:
                    summary_msg = "Audio was detected, but no recognizable speech could be transcribed."

            report_data = {
                "claims": [],
                "summary": summary_msg,
                "overall_risk_score": 0.0
            }
            # Skip Gemini call
        else:
            job.status = AnalysisJobStatus.GENERATING_REPORT
            job.processing_phase = 'Generating report structure (Orchestrated)'
            job.save()

            from .services import AnalysisOrchestrator
            orchestrator = AnalysisOrchestrator()
            report_data = orchestrator.analyze_content(
                ocr_text=ocr_text, 
                transcript_text=transcript_text, 
                analysis_type=job.analysis_type,
                job_id=job.id
            )

        with transaction.atomic():
            # Add raw and structured source data to report_data for rich provenance display
            report_data['ocr_text'] = ocr_text
            report_data['transcript_text'] = transcript_text
            
            # Include structured data if available
            ocr_asset = job.media_assets.filter(asset_type='OCR_RESULTS').first()
            if ocr_asset:
                report_data['ocr_results'] = ocr_asset.metadata
            
            transcript_asset = job.media_assets.filter(asset_type='TRANSCRIPT_RESULTS').first()
            if transcript_asset:
                report_data['transcript_results'] = transcript_asset.metadata
            
            # Fetch search verification references for claims
            # Run in parallel with 3-claim cap to prevent blocking on cloud IP rate limits.
            # Sequential searches could waste 50+ seconds; parallel caps total time at ~2s.
            claims_data = report_data.get('claims', [])
            from concurrent.futures import ThreadPoolExecutor, as_completed

            def _fetch_for_claim(claim_data):
                search_q = claim_data.get('search_query', '')
                if not search_q:
                    claim_data['related_sources'] = []
                    return claim_data
                try:
                    sources = fetch_search_sources(search_q)
                except Exception as ex:
                    logger.error(f"Failed to fetch sources for '{search_q}': {ex}")
                    sources = []
                claim_data['related_sources'] = sources
                return claim_data

            # Only search for the top 3 claims to limit total latency
            searchable = [c for c in claims_data if c.get('search_query')][:3]
            non_searchable = [c for c in claims_data if not c.get('search_query')]

            # Kick off parallel searches
            with ThreadPoolExecutor(max_workers=3) as executor:
                list(executor.map(_fetch_for_claim, searchable))

            # Mark remaining claims with empty sources
            for claim_data in non_searchable:
                claim_data['related_sources'] = []


            # Create Report
            report = AnalysisReport.objects.create(report_data=report_data)
            job.report = report
            
            # Create Claims
            for claim_data in claims_data:
                # Basic validation / normalization
                label = claim_data.get('classification_label', 'UNVERIFIED')
                source = claim_data.get('detection_source', 'TRANSCRIPT')
                
                # fallback for invalid labels
                valid_labels = [c[0] for c in ClaimRecord._meta.get_field('classification_label').choices]
                if label not in valid_labels:
                    label = 'UNVERIFIED'
                    
                valid_sources = [c[0] for c in ClaimRecord._meta.get_field('detection_source').choices]
                if source not in valid_sources:
                    source = 'TRANSCRIPT'
                
                ClaimRecord.objects.create(
                    job=job,
                    claim_text=claim_data.get('claim_text', 'Unknown Claim'),
                    detection_source=source,
                    classification_label=label,
                    confidence_score=claim_data.get('confidence_score', 0.0),
                    contextual_reasoning=claim_data.get('contextual_reasoning', 'No reasoning provided.'),
                    transcript_reference=claim_data.get('transcript_reference', ''),
                    ocr_reference=claim_data.get('ocr_reference', ''),
                    related_sources=claim_data.get('related_sources', [])
                )

            job.status = AnalysisJobStatus.COMPLETED
            job.processing_phase = 'Analysis complete'
            job.save()

        return report.id

    except Exception as e:
        logger.error(f"Analysis failed for job {job_id}: {e}")
        try:
            job = AnalysisJob.objects.get(id=job_id)
            job.status = AnalysisJobStatus.FAILED
            job.error_message = str(e)
            job.save()
        except:
            pass
        raise

# Classification tasks mapped.