const API_BASE = import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000/api';

export const createJob = async (instagramUrl, analysisMode = 'text') => {
  const response = await fetch(`${API_BASE}/jobs/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ instagram_url: instagramUrl, analysis_mode: analysisMode }),
  });
  if (!response.ok) throw new Error('Failed to create job');
  return response.json();
};

export const getJob = async (jobId) => {
  const response = await fetch(`${API_BASE}/jobs/${jobId}/`);
  if (!response.ok) throw new Error('Failed to fetch job');
  return response.json();
};

export const getJobStatus = async (jobId) => {
  const response = await fetch(`${API_BASE}/jobs/${jobId}/status/`);
  if (!response.ok) throw new Error('Failed to fetch job status');
  return response.json();
};

/**
 * Upload a video file to /api/upload/ with progress tracking.
 * @param {File}     file          — the video File object
 * @param {string}   analysisMode  — 'text' | 'audio'
 * @param {Function} onProgress    — optional (pct: 0–100) => void
 * @returns {Promise<Object>}      — job data { id, status, … }
 */
export const uploadMedia = (file, analysisMode = 'text', onProgress = null) =>
  new Promise((resolve, reject) => {
    const form = new FormData();
    form.append('file', file);
    form.append('analysis_mode', analysisMode);

    const xhr = new XMLHttpRequest();
    xhr.open('POST', `${API_BASE}/upload/`);

    if (onProgress) {
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100));
      };
    }

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(JSON.parse(xhr.responseText));
      } else {
        const msg = (() => {
          try { return JSON.parse(xhr.responseText).error || xhr.statusText; }
          catch { return xhr.statusText; }
        })();
        reject(new Error(msg));
      }
    };
    xhr.onerror = () => reject(new Error('Network error during upload.'));
    xhr.send(form);
  });

/**
 * Lightweight fire-and-forget pre-warming ping to wake up sleeping Render backends.
 */
export const pingBackend = async () => {
  try {
    await fetch(`${API_BASE}/health/`, { method: 'GET', cache: 'no-store' });
  } catch {
    // Silently ignore pre-warm connection attempts
  }
};

