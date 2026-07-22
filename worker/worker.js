// ========================================================================
// Cloudflare Workers - Safety Law API (Flask 대체)
// ========================================================================

// ---------- 공통 유틸 ----------
const SAFETY_KEYWORDS = ['안전','재해','산업','중대','노동','사고','보건','위험','산재','중대재해'];

function corsHeaders(origin = '*') {
  return {
    'Access-Control-Allow-Origin': origin,
    'Access-Control-Allow-Methods': 'GET, POST, DELETE, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Access-Control-Max-Age': '86400',
  };
}

function jsonResponse(data, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      'Content-Type': 'application/json; charset=utf-8',
      ...corsHeaders(),
      ...extraHeaders,
    },
  });
}

function stripHtml(str) {
  if (!str) return '';
  return str.replace(/<[^>]*>/g, '').replace(/&quot;/g, '"').replace(/&amp;/g, '&')
            .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&#39;/g, "'").trim();
}

function isValidDate(dateStr) {
  if (!dateStr) return false;
  const d = new Date(dateStr);
  if (isNaN(d.getTime())) return false;
  const now = new Date();
  const yearAgo = new Date(now.getFullYear() - 2, now.getMonth(), now.getDate());
  return d >= yearAgo && d <= new Date(now.getTime() + 86400000);
}

// 문자열 유사도(간단 버전 - Python difflib 대체)
function similarity(a, b) {
  if (!a || !b) return 0;
  a = a.toLowerCase(); b = b.toLowerCase();
  if (a === b) return 1;
  const longer = a.length > b.length ? a : b;
  const shorter = a.length > b.length ? b : a;
  if (longer.length === 0) return 1;
  // Levenshtein 기반 유사도
  const dist = levenshtein(longer, shorter);
  return (longer.length - dist) / longer.length;
}

function levenshtein(a, b) {
  const dp = Array.from({ length: a.length + 1 }, () => new Array(b.length + 1).fill(0));
  for (let i = 0; i <= a.length; i++) dp[i][0] = i;
  for (let j = 0; j <= b.length; j++) dp[0][j] = j;
  for (let i = 1; i <= a.length; i++) {
    for (let j = 1; j <= b.length; j++) {
      dp[i][j] = a[i-1] === b[j-1]
        ? dp[i-1][j-1]
        : 1 + Math.min(dp[i-1][j-1], dp[i-1][j], dp[i][j-1]);
    }
  }
  return dp[a.length][b.length];
}

// ---------- 캐시 헬퍼 (KV 사용) ----------
async function getCache(env, key) {
  const raw = await env.CACHE.get(key);
  return raw ? JSON.parse(raw) : null;
}
async function setCache(env, key, value, ttlSec) {
  await env.CACHE.put(key, JSON.stringify(value), { expirationTtl: ttlSec });
}

// ========================================================================
// 뉴스 API 관련
// ========================================================================

// 카카오 뉴스 검색
async function fetchKakaoNews(keyword, apiKey) {
  try {
    const url = `https://dapi.kakao.com/v2/search/web?query=${encodeURIComponent(keyword)}&size=30&sort=recency`;
    const res = await fetch(url, { headers: { Authorization: `KakaoAK ${apiKey}` } });
    if (!res.ok) return [];
    const data = await res.json();
    const items = data.documents || [];
    return items
      .filter(it => {
        const title = stripHtml(it.title || '');
        return SAFETY_KEYWORDS.some(k => title.includes(k)) && isValidDate(it.datetime);
      })
      .map(it => ({
        title: stripHtml(it.title),
        link: it.url,
        published: it.datetime,
        source: 'Kakao',
      }));
  } catch (e) {
    console.error('kakao error', e);
    return [];
  }
}

// 네이버 뉴스 검색
async function fetchNaverNews(keyword, clientId, clientSecret) {
  try {
    const url = `https://openapi.naver.com/v1/search/news.json?query=${encodeURIComponent(keyword)}&display=30&sort=date`;
    const res = await fetch(url, {
      headers: {
        'X-Naver-Client-Id': clientId,
        'X-Naver-Client-Secret': clientSecret,
      },
    });
    if (!res.ok) return [];
    const data = await res.json();
    return (data.items || [])
      .filter(it => isValidDate(it.pubDate))
      .map(it => ({
        title: stripHtml(it.title),
        link: it.link,
        published: new Date(it.pubDate).toISOString(),
        source: 'Naver',
      }));
  } catch (e) {
    console.error('naver error', e);
    return [];
  }
}

// 구글 뉴스 RSS
async function fetchGoogleNews(keyword) {
  try {
    const url = `https://news.google.com/rss/search?q=${encodeURIComponent(keyword)}&hl=ko&gl=KR&ceid=KR:ko`;
    const res = await fetch(url);
    if (!res.ok) return [];
    const xml = await res.text();
    return parseRss(xml, 'Google').filter(it => isValidDate(it.published));
  } catch (e) {
    console.error('google error', e);
    return [];
  }
}

// RSS XML 파서 (feedparser 대체)
function parseRss(xml, source) {
  const items = [];
  const itemRegex = /<item\b[^>]*>([\s\S]*?)<\/item>/gi;
  let m;
  while ((m = itemRegex.exec(xml)) !== null) {
    const block = m[1];
    const title = stripHtml(extractTag(block, 'title'));
    const link = stripHtml(extractTag(block, 'link'));
    const pub = extractTag(block, 'pubDate');
    if (title && link) {
      items.push({
        title,
        link,
        published: pub ? new Date(pub).toISOString() : new Date().toISOString(),
        source,
      });
    }
  }
  return items;
}

function extractTag(xml, tag) {
  const re = new RegExp(`<${tag}\\b[^>]*>([\\s\\S]*?)<\\/${tag}>`, 'i');
  const m = xml.match(re);
  if (!m) return '';
  // CDATA 처리
  return m[1].replace(/<!
\[CDATA
\[([\s\S]*?)\]
\]>/g, '$1').trim();
}

// 안전 전문지 RSS 헤드라인
async function fetchSafetyHeadlines() {
  const sources = [
    { name: '안전신문', url: 'https://www.safetynews.co.kr/rss/allArticle.xml' },
    { name: '세이프타임즈', url: 'https://www.safetimes.co.kr/rss/allArticle.xml' },
    { name: '매일안전신문', url: 'https://www.idsn.co.kr/rss/allArticle.xml' },
  ];
  const results = [];
  for (const s of sources) {
    try {
      const res = await fetch(s.url, {
        headers: { 'User-Agent': 'Mozilla/5.0 (compatible; SafetyBot/1.0)' },
      });
      if (!res.ok) continue;
      const xml = await res.text();
      const items = parseRss(xml, s.name).slice(0, 3);
      results.push(...items);
    } catch (e) {
      console.error(`RSS ${s.name} error`, e);
    }
  }
  return results;
}

// ========================================================================
// KOSHA 검색 API
// ========================================================================

const LAW_CATEGORY_MAP = {
  '중대재해 처벌 등에 관한 법률': { type: '법률', order: 1 },
  '중대재해 처벌 등에 관한 법률 시행령': { type: '시행령', order: 2 },
  '산업안전보건법': { type: '법률', order: 3 },
  '산업안전보건법 시행령': { type: '시행령', order: 4 },
  '산업안전보건법 시행규칙': { type: '시행규칙', order: 5 },
  '산업안전보건기준에 관한 규칙': { type: '규칙', order: 6 },
  '유해·위험작업의 취업 제한에 관한 규칙': { type: '규칙', order: 7 },
};

function cleanAdmruleTitle(title) {
  return (title || '').replace(/
\[.*?\]
/g, '').trim();
}

function buildLawLink(title) {
  // 제XX조 패턴에서 링크 생성
  const m = title.match(/제(\d+)조/);
  const lawName = Object.keys(LAW_CATEGORY_MAP).find(k => title.includes(k));
  if (!lawName) return `https://www.law.go.kr/lsSc.do?query=${encodeURIComponent(title)}`;
  return `https://www.law.go.kr/lsSc.do?query=${encodeURIComponent(lawName)}`;
}

async function koshaSearch(keyword, apiKey) {
  const url = `https://apis.data.go.kr/B552468/srch/smartSearch?serviceKey=${apiKey}&searchValue=${encodeURIComponent(keyword)}&numOfRows=100&pageNo=1&resultType=json`;
  const res = await fetch(url);
  const text = await res.text();

  // XML 인증 오류 감지
  if (text.trim().startsWith('<')) {
    if (text.includes('SERVICE_KEY') || text.includes('INVALID')) {
      throw new Error('KOSHA API 키 오류');
    }
    throw new Error('KOSHA API 응답 오류');
  }

  let data;
  try { data = JSON.parse(text); }
  catch { throw new Error('KOSHA JSON 파싱 실패'); }

  const items = data?.response?.body?.items?.item || [];
  const list = Array.isArray(items) ? items : [items];

  const laws = [];
  const guides = [];
  const media = [];

  for (const it of list) {
    const title = cleanAdmruleTitle(it.title || '');
    const category = it.category || '';
    const content = it.content || '';
    const link = it.link || buildLawLink(title);

    const matched = Object.keys(LAW_CATEGORY_MAP).find(k => title.includes(k) || category.includes(k));
    if (matched) {
      let priority = LAW_CATEGORY_MAP[matched].order;

      // 특수 우선순위 오버라이드
      let finalContent = content;
      if (keyword === '난간' && matched === '산업안전보건기준에 관한 규칙' && title.includes('제13조')) {
        priority = -1;
      }
      if (keyword === '매뉴얼' && matched === '중대재해 처벌 등에 관한 법률 시행령' && title.includes('제4조')) {
        priority = -1;
        finalContent = '중대재해처벌법 시행령 제4조에 따른 안전보건 관리체계 구축 및 이행에 관한 매뉴얼/대응지침 관련 조항입니다. 사업주는 안전보건 확보의무를 위한 매뉴얼을 마련하고 이행 여부를 점검해야 합니다.';
      }

      laws.push({ title, category: matched, content: finalContent, link, priority });
    } else if (category.includes('가이드') || category.includes('지침')) {
      guides.push({ title, category, content, link });
    } else if (category.includes('영상') || category.includes('미디어')) {
      media.push({ title, category, content, link });
    }
  }

  laws.sort((a, b) => a.priority - b.priority);

  return {
    law: laws,
    guide: guides,
    media,
    total_media: media.length,
  };
}

// ========================================================================
// 라우터
// ========================================================================

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname;
    const method = request.method;

    // CORS Preflight
    if (method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: corsHeaders() });
    }

    try {
      // 헬스체크
      if (path === '/' || path === '/api') {
        return jsonResponse({ status: 'ok', service: 'safety-law-api', platform: 'cloudflare-workers' });
      }

      // 뉴스
      if (path === '/api/news' && method === 'GET') {
        const cached = await getCache(env, 'news:all');
        if (cached) return jsonResponse(cached);

        const [google, kakao, naver] = await Promise.all([
          fetchGoogleNews('고용노동부 안전 OR 중대재해 OR 산업재해'),
          fetchKakaoNews('산업재해 중대재해', env.KAKAO_API_KEY),
          fetchNaverNews('중대재해 사망', env.NAVER_CLIENT_ID, env.NAVER_CLIENT_SECRET),
        ]);

        const all = [...google, ...kakao, ...naver];

        // 중복 제거 (유사도 0.7 이상)
        const unique = [];
        for (const item of all) {
          const dup = unique.find(u => similarity(u.title, item.title) >= 0.7);
          if (!dup) unique.push(item);
        }
        unique.sort((a, b) => new Date(b.published) - new Date(a.published));
        const result = unique.slice(0, 50);

        await setCache(env, 'news:all', result, 300); // 5분
        return jsonResponse(result);
      }

      // 안전 헤드라인
      if (path === '/api/safety_headlines' && method === 'GET') {
        const cached = await getCache(env, 'safety:headlines');
        if (cached) return jsonResponse(cached);
        const result = await fetchSafetyHeadlines();
        await setCache(env, 'safety:headlines', result, 600); // 10분
        return jsonResponse(result);
      }

      // 방문자 카운트
      if (path === '/api/visit' && method === 'GET') {
        const today = new Date().toISOString().slice(0, 10);
        const totalKey = 'visit:total';
        const todayKey = `visit:${today}`;
        const total = parseInt(await env.CACHE.get(totalKey) || '0') + 1;
        const daily = parseInt(await env.CACHE.get(todayKey) || '0') + 1;
        await env.CACHE.put(totalKey, String(total));
        await env.CACHE.put(todayKey, String(daily), { expirationTtl: 86400 * 2 });
        return jsonResponse({ today: daily, total });
      }

      // KOSHA 검색
      if (path === '/api/search' && method === 'GET') {
        const q = url.searchParams.get('q') || '';
        if (!q) return jsonResponse({ error: '검색어가 필요합니다' }, 400);
        try {
          const result = await koshaSearch(q, env.KOSHA_API_KEY);
          return jsonResponse(result);
        } catch (e) {
          return jsonResponse({ error: e.message }, 500);
        }
      }

      // Q&A 목록/작성
      if (path === '/api/qna') {
        if (method === 'GET') {
          const qs = await env.DB.prepare(
            'SELECT * FROM questions ORDER BY created_at DESC LIMIT 100'
          ).all();
          const questions = qs.results || [];
          for (const q of questions) {
            const ans = await env.DB.prepare(
              'SELECT * FROM answers WHERE question_id = ? ORDER BY created_at ASC'
            ).bind(q.id).all();
            q.answers = ans.results || [];
          }
          return jsonResponse(questions);
        }
        if (method === 'POST') {
          const body = await request.json();
          const content = (body.content || '').trim();
          const author = (body.author || '익명').trim() || '익명';
          if (!content) return jsonResponse({ error: '내용을 입력하세요' }, 400);
          const r = await env.DB.prepare(
            'INSERT INTO questions (author, content) VALUES (?, ?)'
          ).bind(author, content).run();
          return jsonResponse({ id: r.meta.last_row_id, ok: true });
        }
      }

      // Q&A 답변 작성
      const replyMatch = path.match(/^\/api\/qna\/(\d+)\/reply$/);
      if (replyMatch && method === 'POST') {
        const qId = parseInt(replyMatch[1]);
        const body = await request.json();
        const content = (body.content || '').trim();
        const author = (body.author || '익명').trim() || '익명';
        if (!content) return jsonResponse({ error: '내용을 입력하세요' }, 400);
        await env.DB.prepare(
          'INSERT INTO answers (question_id, author, content) VALUES (?, ?, ?)'
        ).bind(qId, author, content).run();
        return jsonResponse({ ok: true });
      }

      // Q&A 질문 삭제
      const qDelMatch = path.match(/^\/api\/qna\/question\/(\d+)$/);
      if (qDelMatch && method === 'DELETE') {
        const body = await request.json().catch(() => ({}));
        if (body.password !== env.ADMIN_PASSWORD) {
          return jsonResponse({ error: '비밀번호 오류' }, 403);
        }
        const qId = parseInt(qDelMatch[1]);
        await env.DB.prepare('DELETE FROM answers WHERE question_id = ?').bind(qId).run();
        await env.DB.prepare('DELETE FROM questions WHERE id = ?').bind(qId).run();
        return jsonResponse({ ok: true });
      }

      // Q&A 답변 삭제
      const aDelMatch = path.match(/^\/api\/qna\/answer\/(\d+)$/);
      if (aDelMatch && method === 'DELETE') {
        const body = await request.json().catch(() => ({}));
        if (body.password !== env.ADMIN_PASSWORD) {
          return jsonResponse({ error: '비밀번호 오류' }, 403);
        }
        const aId = parseInt(aDelMatch[1]);
        await env.DB.prepare('DELETE FROM answers WHERE id = ?').bind(aId).run();
        return jsonResponse({ ok: true });
      }

      return jsonResponse({ error: 'Not Found', path }, 404);
    } catch (e) {
      console.error('Worker error:', e);
      return jsonResponse({ error: e.message || '서버 오류' }, 500);
    }
  },
};
