// k6 load generator for the i-dot-ai-utilities logging sandbox.
//
// Hits two targets with a weighted mix of endpoints so the log stream
// contains a realistic blend of info / warning / error messages:
//
//   - FastAPI demo (:8001) - ContextEnrichmentType.FASTAPI
//   - Django demo  (:8003) - StructuredLoggingMiddlewareOTel + DjangoUserIdMiddleware
//
// Parametrised via env vars so the same script drives the automated compose
// run and ad-hoc manual runs (see sandbox/README.md for examples).

import http from 'k6/http';
import { sleep } from 'k6';

export const options = {
    vus: parseInt(__ENV.K6_VUS || '5', 10),
    duration: __ENV.K6_DURATION || '5m',
};

const FASTAPI = __ENV.FASTAPI_URL || 'http://fastapi-app:8001';
const DJANGO  = __ENV.DJANGO_URL  || 'http://django-app:8003';

// A target is (base_url, is_django) so URL normalisation can add trailing
// slashes only where Django expects them.
const TARGETS = [
    { url: FASTAPI, django: false, tag: 'fastapi' },
    { url: DJANGO,  django: true,  tag: 'django'  },
];

// Return a random hex string that is a valid W3C traceparent.
function traceparent() {
    const hex = (n) => {
        let s = '';
        for (let i = 0; i < n; i++) {
            s += Math.floor(Math.random() * 16).toString(16);
        }
        return s;
    };
    return `00-${hex(32)}-${hex(16)}-01`;
}

function weightedPick() {
    const r = Math.random();
    if (r < 0.40) return '/';
    if (r < 0.65) return `/users/${Math.floor(Math.random() * 1000)}`;
    if (r < 0.80) return `/search?q=${['logs','error','trace','demo'][Math.floor(Math.random()*4)]}`;
    if (r < 0.90) return '/slow';
    return '/boom';
}

function normaliseForDjango(path) {
    if (!path.startsWith('/search') && !path.endsWith('/')) {
        const [p, q] = path.split('?');
        return q ? `${p}/?${q}` : `${p}/`;
    }
    if (path.startsWith('/search?')) {
        return path.replace('/search?', '/search/?');
    }
    return path;
}

export default function () {
    const target = TARGETS[Math.floor(Math.random() * TARGETS.length)];
    let path = weightedPick();
    if (target.django) {
        path = normaliseForDjango(path);
    }

    const params = {
        headers: {
            'User-Agent': 'k6-sandbox/1.0',
        },
        tags: { app: target.tag },
    };

    // Inject a traceparent on ~50% of requests so you can see header-based
    // trace propagation working. The OTel Django middleware's composite
    // propagator extracts and continues the trace.
    if (Math.random() < 0.5) {
        params.headers['traceparent'] = traceparent();
    }

    // Exercise the header allowlist on the Django app.
    if (target.django && Math.random() < 0.3) {
        params.headers['X-Tenant-ID'] = `tenant-${Math.floor(Math.random() * 5)}`;
    }

    http.get(`${target.url}${path}`, params);

    sleep(0.4 + Math.random() * 0.6);
}
