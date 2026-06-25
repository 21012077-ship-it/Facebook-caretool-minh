"""fb_scraper.py — Facebook page scraping utilities dùng Playwright.

v3 — Fix critical JS bugs:
- Removed 'const root = arguments[0]' in arrow function context (SyntaxError)
- Fixed multiline regex literals (invalid JS syntax)
- All JS strings are now syntactically valid single-expression functions
- Multi-strategy fallback: data-ad-preview > dir=auto > innerText > page-level
"""
from __future__ import annotations

import re
import random
import time
from typing import Any

# ---------------------------------------------------------------------------
# 1. Tìm phần tử bài viết chính (có scoring)
#    Wrap trong IIFE vì không nhận element argument
# ---------------------------------------------------------------------------

_JS_FIND_MAIN_POST = """
(function() {
    var isHidden = function(el) {
        var s = window.getComputedStyle(el);
        return s.display === 'none' || s.visibility === 'hidden';
    };
    // Ưu tiên dialog/popup
    var dialogs = Array.from(document.querySelectorAll('div[role="dialog"]'))
        .filter(function(el) { return !isHidden(el); });
    if (dialogs.length > 0) {
        return { type: 'dialog', index: dialogs.length - 1 };
    }
    // Tìm article tốt nhất theo score
    var articles = Array.from(document.querySelectorAll('div[role="article"]'))
        .filter(function(el) {
            if (isHidden(el)) return false;
            // Loại bỏ article trong navigation/banner THỰC SỰ (không dùng aria-label FB)
            var inNav = el.closest('nav, header, [role="navigation"], [role="banner"]');
            return !inNav;
        });
    if (articles.length === 0) return { type: 'main', index: 0 };
    var scored = articles.map(function(art, idx) {
        var text = art.innerText || art.textContent || '';
        var wordCount = (text.match(/[\\wÀ-ỹ#]+/g) || []).length;
        var mediaCount = art.querySelectorAll('img[src], video').length;
        var hasBox = art.querySelector('[contenteditable="true"][role="textbox"]') ? 1 : 0;
        var rect = art.getBoundingClientRect();
        var vBonus = (rect.top > -500 && rect.top < window.innerHeight + 500) ? 150 : 0;
        return { index: idx, score: wordCount * 8 + mediaCount * 20 + vBonus + hasBox * 80, len: text.length };
    }).filter(function(c) { return c.len > 5; });
    if (scored.length === 0) return { type: 'article', index: 0 };
    scored.sort(function(a, b) { return b.score - a.score; });
    return { type: 'article', index: scored[0].index };
})()
"""

# ---------------------------------------------------------------------------
# 2. Extract post data — nhận element argument, không dùng arguments[0]
#    Dùng regular function (không phải arrow) để tránh vấn đề arguments
# ---------------------------------------------------------------------------

_JS_EXTRACT_POST_DATA_FN = """
function(root) {
    var norm = function(v) { return String(v || '').replace(/\\s+/g, ' ').trim(); };

    // Noise patterns — KHÔNG dùng multiline regex literal
    var actionNoise = new RegExp(
        '^(th\\u00edch|like|b\\u00ecnh lu\\u1eadn|comment|chia s\\u1ebb|share|' +
        'g\\u1eedi|send|ph\\u1ea3n h\\u1ed3i|reply|theo d\\u00f5i|follow|' +
        'x\\u1ebem th\\u00eam|see more|\\u1ea9n b\\u1edbt|see less|' +
        'most relevant|ph\\u00f9 h\\u1ee3p nh\\u1ea5t)$', 'i'
    );
    var metaNoise = new RegExp(
        '^(\\\\d+\\\\s*(gi\\u00e2y|ph\\u00fat|gi\\u1edd|ng\\u00e0y|tu\\u1ea7n|' +
        'th\\u00e1ng|n\\u0103m|s|m|h|d|w|mo|y)\\\\s*(tr\\u01b0\\u1edbc)?|' +
        'v\\u1eeba xong|just now|top fan|author|fan c\\u1ee9ng)$', 'i'
    );
    var counterNoise = /^\\d+[.,]?\\d*\\s*(k|m|n|tr)?\\s*(th\\u00edch|likes?|b\\u00ecnh lu\\u1eadn|comments?|shares?|ph\\u1ea3n h\\u1ed3i|repl(y|ies))$/i;

    var isNoise = function(t) {
        if (!t || t.length < 2) return true;
        return actionNoise.test(t) || metaNoise.test(t) || counterNoise.test(t);
    };

    var postText = '';
    var accountName = '';

    // Chiến lược 1: selector chuyên dụng Facebook
    var specials = [
        'div[data-ad-preview="message"]',
        'div[data-testid="post_message"]',
        '[data-ad-comet-preview="message"]'
    ];
    for (var si = 0; si < specials.length; si++) {
        var msgEl = root.querySelector(specials[si]);
        if (msgEl) {
            var t1 = norm(msgEl.innerText || msgEl.textContent || '');
            if (t1.length >= 10) { postText = t1; break; }
        }
    }

    // Chiến lược 2: div[dir="auto"] và span[dir="auto"] — CHỈ trong phần post body, KHÔNG lấy comment
    if (!postText || postText.length < 10) {
        // Xác định vùng BODY của bài (trước khi comment bắt đầu)
        // Comment trong FB nằm trong div[role="article"] con, hoặc ul chứa comment
        // Hàm kiểm tra xem element có nằm trong comment không
        var isInCommentSection = function(el) {
            // Mọi div[role="article"] con đều là comment (article cha là bài viết chính)
            var par = el.parentElement;
            while (par && par !== root) {
                if (par.getAttribute('role') === 'article') return true;
                // ul chứa comment có aria-label "Bình luận" hoặc "Comments"
                if (par.tagName === 'UL') return true;
                // FB đôi khi dùng aria-label="Comment" trên container
                var ariaL = (par.getAttribute('aria-label') || '').toLowerCase();
                if (ariaL.indexOf('comment') >= 0 || ariaL.indexOf('b\u00ecnh lu\u1eadn') >= 0) return true;
                par = par.parentElement;
            }
            return false;
        };

        var dirEls = Array.from(root.querySelectorAll('div[dir="auto"], span[dir="auto"]'))
            .filter(function(el) {
                var cs = window.getComputedStyle(el);
                if (cs.display === 'none' || cs.visibility === 'hidden') return false;
                if (el.closest('[role="button"]') || el.closest('nav') || el.closest('header') || el.closest('[role="navigation"]') || el.closest('[role="complementary"]') || el.closest('[role="banner"]')) return false;
                // LOẠI TRỪ phần comment
                if (isInCommentSection(el)) return false;
                var t = (el.innerText || el.textContent || '').trim();
                return t.length > 8 && !isNoise(t);
            });
        var dirTexts = dirEls.map(function(el) { return norm(el.innerText || el.textContent || ''); });
        // Loại trùng
        var seen = {};
        var unique = dirTexts.filter(function(t) {
            if (seen[t]) return false;
            seen[t] = 1;
            return !isNoise(t);
        });
        unique.sort(function(a, b) { return b.length - a.length; });
        if (unique.length > 0) {
            postText = unique.slice(0, 8).join('\\n').slice(0, 5000);
        }
    }

    // Chiến lược 3: innerText toàn root — lấy TEXT THUẦN của root nhưng bỏ comment articles
    if (!postText || postText.length < 10) {
        // Clone node rồi xóa comment articles trước khi lấy innerText
        var tempRoot = root.cloneNode(true);
        // Xóa tất cả div[role="article"] con (comment)
        var commentArts = Array.from(tempRoot.querySelectorAll('div[role="article"]'));
        commentArts.forEach(function(a) { a.remove(); });
        // Xóa ul (comment list)
        var commentUls = Array.from(tempRoot.querySelectorAll('ul'));
        commentUls.forEach(function(u) { u.remove(); });
        var rawText = (tempRoot.innerText || tempRoot.textContent || '').trim();
        if (rawText.length > 10) {
            var lines = rawText.split('\\n')
                .map(norm)
                .filter(function(l) { return l.length > 5 && !isNoise(l); });
            var uniqLines = [];
            var seenL = {};
            for (var li = 0; li < lines.length && uniqLines.length < 40; li++) {
                if (!seenL[lines[li]]) { seenL[lines[li]] = 1; uniqLines.push(lines[li]); }
            }
            postText = uniqLines.join('\\n').slice(0, 5000);
        }
    }


    // Lấy accountName
    var nameEls = Array.from(root.querySelectorAll(
        'h1 a, h2 a, h3 a, strong a, a[role="link"]'
    ));
    for (var ni = 0; ni < nameEls.length; ni++) {
        var nt = norm(nameEls[ni].innerText || nameEls[ni].getAttribute('aria-label') || nameEls[ni].textContent || '');
        if (nt && nt.length >= 2 && nt.length <= 80 && !isNoise(nt) && !/^#/.test(nt)) {
            accountName = nt;
            break;
        }
    }

    // Hashtags
    var hashMatches = postText.match(/#[\\p{L}\\p{N}_]+/gu) || [];
    var hashSeen = {};
    var hashtags = hashMatches.filter(function(h) {
        if (hashSeen[h]) return false;
        hashSeen[h] = 1;
        return true;
    }).slice(0, 15);

    // Media alt text — CHI lay img[alt] that su, khong lay aria-label buttons
    // (aria-label tren Facebook rat nhieu: "Dong", "Hanh dong doi voi bai viet"...)
    var mediaEls = Array.from(root.querySelectorAll('img[alt]'));
    var mediaTexts = [];
    var mediaSeen = {};
    var mediaNoiseRe = /^(image may contain|anh co the chua|dong|close|like|share|comment|xem them|see more|play|pause|mute|video|photo|hinh|video|facebook|messenger|logo)/i;
    for (var mi = 0; mi < mediaEls.length; mi++) {
        var mt = norm(mediaEls[mi].getAttribute('alt') || '');
        if (mt && mt.length >= 8 && !mediaSeen[mt] && !mediaNoiseRe.test(mt)) {
            // Loai tru ten page (thường lap lai tu accountName)
            if (accountName && mt.toLowerCase().indexOf(accountName.toLowerCase().slice(0,10)) >= 0) continue;
            mediaSeen[mt] = 1;
            mediaTexts.push(mt);
            if (mediaTexts.length >= 5) break;
        }
    }


    return {
        accountName: accountName,
        postText: postText,
        hashtags: hashtags,
        domMediaText: mediaTexts.join('\\n')
    };
}
"""

# ---------------------------------------------------------------------------
# 3. Page-level fallback — không nhận argument, dùng trực tiếp document
# ---------------------------------------------------------------------------

_JS_PAGE_FALLBACK_TEXT = """
(function() {
    var norm = function(v) { return String(v || '').replace(/\\s+/g, ' ').trim(); };
    var actionNoise = /^(th\\u00edch|like|b\\u00ecnh lu\\u1eadn|comment|chia s\\u1ebb|share|ph\\u1ea3n h\\u1ed3i|reply|x\\u1ebem th\\u00eam|see more|th\\u00f4ng b\\u00e1o|t\\u1ea5t c\\u1ea3|ch\\u01b0a \\u0111\\u1ecdc)$/i;
    var metaNoise = /^(\\d+\\s*(gi\\u00e2y|ph\\u00fat|gi\\u1edd|ng\\u00e0y|s|m|h|d|w)\\s*(tr\\u01b0\\u1edbc)?|v\\u1eeba xong|just now|b\\u1ea1n \\u0111\\u00e3 t\\u1eaft|b\\u1eadt th\\u00f4ng b\\u00e1o|l\\u00fac kh\\u00e1c)$/i;

    // Ưu tiên dialog
    var dialog = document.querySelector('div[role="dialog"]');
    var mainEl = document.querySelector('[role="main"]') || document.body;
    var target = dialog || mainEl;

    var els = Array.from(target.querySelectorAll('div[dir="auto"], span[dir="auto"]'))
        .filter(function(el) {
            if (el.closest('[role="button"]') || el.closest('nav') || el.closest('header') || el.closest('[role="navigation"]') || el.closest('[role="complementary"]') || el.closest('[role="banner"]')) return false;
            var cs = window.getComputedStyle(el);
            if (cs.display === 'none') return false;
            var t = (el.innerText || el.textContent || '').trim();
            return t.length > 8 && !actionNoise.test(t) && !metaNoise.test(t);
        })
        .map(function(el) { return norm(el.innerText || el.textContent || ''); });

    var seen = {};
    var unique = els.filter(function(t) {
        if (seen[t]) return false;
        seen[t] = 1;
        return true;
    });
    unique.sort(function(a, b) { return b.length - a.length; });
    return unique.slice(0, 8).join('\\n').slice(0, 5000);
})()
"""

# ---------------------------------------------------------------------------
# 4. Quét comment để reply
# ---------------------------------------------------------------------------

_JS_FIND_REPLY_TARGET = """
function(root) {
    var norm = function(v) { return String(v || '').replace(/\\s+/g, ' ').trim(); };
    // Pattern: "X phản hồi" hoặc "X replies" — nhận biết comment có reply
    var hasRepliesRe = /\\b\\d+\\s*(ph\\u1ea3n h\\u1ed3i|repl(y|ies)|tr\\u1ea3 l\\u1eddi)/i;
    // Noise patterns cho text extraction
    var actionNoise = /^(th\\u00edch|like|b\\u00ecnh lu\\u1eadn|comment|chia s\\u1ebb|share|ph\\u1ea3n h\\u1ed3i|reply|tr\\u1ea3 l\\u1eddi|x\\u1ebem th\\u00eam|see more)$/i;
    var metaNoise = /^(\\d+\\s*(gi\\u00e2y|ph\\u00fat|gi\\u1edd|ng\\u00e0y|s|m|h|d|w)\\s*(tr\\u01b0\\u1edbc)?|v\\u1eeba xong|just now|top fan|author|fan c\\u1ee9ng)$/i;
    var counterRe = /^\\d+[.,]?\\d*\\s*(k|m)?\\s*(th\\u00edch|likes?|ph\\u1ea3n h\\u1ed3i|repl(y|ies))$/i;
    var replyLabelRe = /(xem|view|see|\\u1ea9n|hide)?\\s*\\d+[.,]?\\d*\\s*(ph\\u1ea3n h\\u1ed3i|repl(y|ies)|tr\\u1ea3 l\\u1eddi)/i;

    var isNoise = function(t) {
        return !t || t.length < 3 || actionNoise.test(t) || metaNoise.test(t) || counterRe.test(t) || replyLabelRe.test(t);
    };

    var searchRoot = root || document;

    // Dùng div[role="article"] làm ranh giới comment — tránh text tràn sang comment kề bên
    var allArticles = Array.from(searchRoot.querySelectorAll('div[role="article"]'));
    // Bỏ qua article đầu tiên (bài viết chính)
    var commentArts = allArticles.length > 1 ? allArticles.slice(1) : allArticles;

    for (var i = 0; i < Math.min(commentArts.length, 40); i++) {
        var art = commentArts[i];
        var artCS = window.getComputedStyle(art);
        if (artCS.display === 'none' || artCS.visibility === 'hidden') continue;

        // Kiểm tra article này có "X phản hồi" không
        var artFullText = (art.innerText || art.textContent || '').replace(/\\s+/g, ' ');
        if (!hasRepliesRe.test(artFullText)) continue;

        // Lấy text CHỈ trong article này (không tràn ra ngoài)
        var dirEls = Array.from(art.querySelectorAll('div[dir="auto"], span[dir="auto"]'))
            .filter(function(el) {
                if (el.closest('[role="button"]')) return false;
                var s = window.getComputedStyle(el);
                return s.display !== 'none' && s.visibility !== 'hidden';
            });
        var bestText = dirEls
            .map(function(el) { return norm(el.innerText || el.textContent || ''); })
            .filter(function(t) { return !isNoise(t); })
            .sort(function(a, b) { return b.length - a.length; })[0] || '';

        if (!bestText || bestText.length < 3) continue;

        // Tìm button "Phản hồi" / "Reply" TRONG CHÍNH article này
        var replyBtn = null;
        var artBtns = Array.from(art.querySelectorAll('div[role="button"], span[role="button"]'));
        for (var j = 0; j < artBtns.length; j++) {
            var bt = artBtns[j];
            var bcs = window.getComputedStyle(bt);
            if (bcs.display === 'none' || bcs.visibility === 'hidden') continue;
            var btext = norm(bt.innerText || bt.textContent || '');
            if (/^(ph\\u1ea3n h\\u1ed3i|reply|tr\\u1ea3 l\\u1eddi)$/i.test(btext)) {
                replyBtn = bt;
                break;
            }
        }

        if (!replyBtn) continue;

        // Đánh dấu button để Python click đúng
        var uid = 'rbt-' + i + '-' + Date.now();
        replyBtn.setAttribute('data-reply-uid', uid);

        return {
            text: bestText.replace(/^(fan c\\u1ee9ng|top fan|author|t\\u00e1c gi\\u1ea3)\\s*/i, '').slice(0, 1200),
            uid: uid
        };
    }
    return null;
}
"""


# ---------------------------------------------------------------------------
# 5. Quét comment đầu tiên (fallback)
# ---------------------------------------------------------------------------

_JS_FIND_FIRST_COMMENT = """
function(root) {
    var norm = function(v) { return String(v || '').replace(/\\s+/g, ' ').trim(); };
    var actionNoise = /^(th\\u00edch|like|b\\u00ecnh lu\\u1eadn|comment|chia s\\u1ebb|share|ph\\u1ea3n h\\u1ed3i|reply|tr\\u1ea3 l\\u1eddi|x\\u1ebem th\\u00eam|see more)$/i;
    var metaNoise = /^(\\d+\\s*(gi\\u00e2y|ph\\u00fat|gi\\u1edd|ng\\u00e0y|s|m|h|d|w)\\s*(tr\\u01b0\\u1edbc)?|v\\u1eeba xong|just now|top fan|author|fan c\\u1ee9ng)$/i;
    var counterRe = /^\\d+[.,]?\\d*\\s*(k|m)?\\s*(th\\u00edch|likes?|b\\u00ecnh lu\\u1eadn|comments?|ph\\u1ea3n h\\u1ed3i|repl(y|ies))$/i;

    var isOk = function(t) { return t && t.length >= 4 && !actionNoise.test(t) && !metaNoise.test(t) && !counterRe.test(t); };

    var searchRoot = root || document;

    // Thử aria-label comment trước
    var commentSelectors = ['div[aria-label*="Comment by" i]', 'div[aria-label*="B\\u00ecnh lu\\u1eadn c\\u1ee7a" i]'];
    for (var si = 0; si < commentSelectors.length; si++) {
        var nodes = Array.from(searchRoot.querySelectorAll(commentSelectors[si]));
        for (var ni = 0; ni < Math.min(nodes.length, 10); ni++) {
            var dirEls = Array.from(nodes[ni].querySelectorAll('div[dir="auto"], span[dir="auto"]'))
                .filter(function(el) { return !el.closest('[role="button"]'); });
            var best = dirEls.map(function(el) { return norm(el.innerText || el.textContent || ''); })
                .filter(isOk).sort(function(a, b) { return b.length - a.length; })[0] || '';
            if (best) return best.replace(/^(fan c\\u1ee9ng|top fan|author)\\s*/i, '').slice(0, 1200);
        }
    }

    // Fallback: article con (bỏ qua article đầu = bài chính)
    var arts = Array.from(searchRoot.querySelectorAll('div[role="article"]'));
    var commentArts = arts.length > 1 ? arts.slice(1) : arts;
    for (var ai = 0; ai < Math.min(commentArts.length, 15); ai++) {
        var cs = window.getComputedStyle(commentArts[ai]);
        if (cs.display === 'none' || cs.visibility === 'hidden') continue;
        var dirEls2 = Array.from(commentArts[ai].querySelectorAll('div[dir="auto"], span[dir="auto"]'))
            .filter(function(el) { return !el.closest('[role="button"]'); });
        var best2 = dirEls2.map(function(el) { return norm(el.innerText || el.textContent || ''); })
            .filter(isOk).sort(function(a, b) { return b.length - a.length; })[0] || '';
        if (best2) return best2.replace(/^(fan c\\u1ee9ng|top fan|author)\\s*/i, '').slice(0, 1200);
    }
    return '';
}
"""


# ---------------------------------------------------------------------------
# Python API
# ---------------------------------------------------------------------------

def find_main_post_element(page: Any) -> Any:
    """Tìm element bài viết chính. Trả về Playwright Locator."""
    try:
        result = page.evaluate(_JS_FIND_MAIN_POST)
    except Exception:
        result = {"type": "main", "index": 0}

    post_type = result.get("type", "main") if isinstance(result, dict) else "main"
    index = result.get("index", 0) if isinstance(result, dict) else 0

    try:
        if post_type == "dialog":
            dialogs = page.locator('div[role="dialog"]')
            count = dialogs.count()
            idx = min(index, max(0, count - 1))
            return dialogs.nth(idx)
        elif post_type == "article":
            articles = page.locator('div[role="article"]')
            count = articles.count()
            if count > 0:
                idx = min(index, count - 1)
                return articles.nth(idx)
    except Exception:
        pass

    # Fallback: vùng main content
    try:
        main = page.locator('[role="main"]')
        if main.count() > 0:
            return main.first
    except Exception:
        pass
    return page.locator("body").first


def expand_post_text(page: Any, post_element: Any) -> None:
    """Click 'Xem thêm' / 'See more' để mở rộng caption bài.
    Dùng count() kiểm tra trước để tránh timeout không cần thiết.
    """
    see_more_selector = (
        'div[role="button"]:has-text("Xem thêm"), '
        'div[role="button"]:has-text("See more"), '
        'span:has-text("Xem thêm"), '
        'span:has-text("See more")'
    )
    for _round in range(2):
        try:
            buttons = post_element.locator(see_more_selector)
            count = buttons.count()
            if count == 0:
                break
            clicked = False
            for i in range(min(count, 3)):
                try:
                    btn = buttons.nth(i)
                    if btn.is_visible(timeout=300):
                        btn.click(timeout=1500)
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                break
            time.sleep(0.4)
        except Exception:
            break


def extract_post_data(post_element: Any) -> dict:
    """Trích xuất thông tin bài viết: accountName, postText, hashtags, domMediaText."""
    empty: dict = {"accountName": "", "postText": "", "hashtags": [], "domMediaText": ""}
    try:
        result = post_element.evaluate(_JS_EXTRACT_POST_DATA_FN)
        if isinstance(result, dict) and result.get("postText"):
            return result
    except Exception as exc:
        # Log error vào stderr để debug
        import sys
        print(f"[fb_scraper] extract_post_data error: {exc}", file=sys.stderr)
    return empty


def extract_post_data_from_page(page: Any) -> str:
    """Fallback: lấy text bài viết trực tiếp từ page khi post_element không work."""
    try:
        return page.evaluate(_JS_PAGE_FALLBACK_TEXT) or ""
    except Exception as exc:
        import sys
        print(f"[fb_scraper] page_fallback error: {exc}", file=sys.stderr)
        return ""


def scan_reply_target(page: Any, post_element: Any) -> str:
    """Quét comment cần reply. Trả về text comment hoặc '' nếu không tìm thấy.

    Deprecated: Dùng scan_and_click_reply_target() để đảm bảo scan và click cùng 1 comment.
    """
    result = scan_and_click_reply_target(page, post_element, click=False)
    return result[0] if result else ""


def scan_and_click_reply_target(page: Any, post_element: Any, click: bool = True) -> tuple[str, bool]:
    """Quét comment có phản hồi VÀ click Reply button của đúng comment đó.

    Trả về (comment_text, clicked).
    - comment_text: text của comment được chọn ('' nếu không tìm thấy)
    - clicked: True nếu đã click Reply button thành công (chỉ khi click=True)

    Đây là hàm cốt lõi: scan và click CÙNG 1 comment, không bị mismatch.
    """
    for _round in range(3):
        try:
            result = post_element.evaluate(_JS_FIND_REPLY_TARGET)
            if isinstance(result, dict) and result.get('text'):
                text = _sanitize(result['text'])
                uid = result.get('uid', '')
                if not text:
                    continue
                if not click:
                    return (text, False)
                # Click đúng button đã được đánh dấu uid
                try:
                    btn_locator = page.locator(f'[data-reply-uid="{uid}"]')
                    if btn_locator.count() > 0 and btn_locator.first.is_visible(timeout=2000):
                        btn_locator.first.scroll_into_view_if_needed()
                        time.sleep(random.uniform(0.3, 0.6))
                        btn_locator.first.click(timeout=3000)
                        time.sleep(random.uniform(0.5, 1.0))
                        return (text, True)
                except Exception:
                    pass
                # Fallback click: dùng JS click trực tiếp qua uid
                try:
                    page.evaluate(f"document.querySelector('[data-reply-uid="+chr(34)+uid+chr(34)+"]')?.click()")
                    time.sleep(random.uniform(0.5, 1.0))
                    return (text, True)
                except Exception:
                    pass
                return (text, False)
        except Exception:
            pass
        try:
            page.mouse.wheel(0, random.randint(400, 700))
        except Exception:
            pass
        time.sleep(random.uniform(0.4, 0.8))

    # Fallback: comment đầu tiên
    text = find_first_comment_text(post_element)
    return (text, False)


def find_first_comment_text(post_element: Any) -> str:
    """Lấy text của comment đầu tiên nhìn thấy."""
    try:
        text = post_element.evaluate(_JS_FIND_FIRST_COMMENT)
        return _sanitize(text)
    except Exception:
        return ""


def build_full_post_context(page: Any, post_element: Any) -> str:
    """Gộp toàn bộ nội dung bài viết thành chuỗi duy nhất để gửi cho AI.

    Tự động fallback sang page-level scan nếu post_element không trả về text.
    """
    data = extract_post_data(post_element)
    post_text = (data.get("postText") or "").strip()

    # Fallback: scan toàn trang nếu post_element không lấy được text
    if not post_text or len(post_text) < 8:
        fallback = extract_post_data_from_page(page)
        if fallback and len(fallback) > len(post_text):
            post_text = fallback
            data = {
                "accountName": data.get("accountName", ""),
                "postText": post_text,
                "hashtags": [],
                "domMediaText": "",
            }

    if not post_text:
        return ""

    parts = []
    if data.get("accountName"):
        parts.append(f"[Page: {data['accountName']}]")
    parts.append(post_text)
    if data.get("hashtags"):
        parts.append(f"[Hashtags: {' '.join(data['hashtags'])}]")
    if data.get("domMediaText"):
        parts.append(f"[Media: {data['domMediaText']}]")
    return "\n".join(parts).strip()


def _sanitize(text: str | None) -> str:
    """Normalize và lọc prefix badge."""
    text = re.sub(r"\s+", " ", (text or "")).strip()
    text = re.sub(r"^(Fan cứng|Top fan|Author)\s+", "", text, flags=re.IGNORECASE)
    return text[:1200]


# ---------------------------------------------------------------------------
# JS: Tìm ảnh chính trong bài viết (sắp xếp theo kích thước, lớn nhất trước)
# ---------------------------------------------------------------------------

_JS_EXTRACT_POST_IMAGES = """
function(root) {
    var result = [];
    var seen = {};

    function addImg(src, w, h) {
        if (!src || seen[src]) return;
        // Bo qua data URI, blob, SVG, placeholder nho
        if (src.startsWith('data:') || src.startsWith('blob:') || src.indexOf('.svg') >= 0) return;
        if (w < 60 || h < 60) return;
        seen[src] = 1;
        result.push({ src: src, w: Math.round(w), h: Math.round(h) });
    }

    function getSize(el) {
        var rect = el.getBoundingClientRect();
        var w = rect.width > 0 ? rect.width : (el.naturalWidth || el.offsetWidth || 0);
        var h = rect.height > 0 ? rect.height : (el.naturalHeight || el.offsetHeight || 0);
        return [w, h];
    }

    // --- 1. Quet tat ca the img (bao gom lazy-load dung currentSrc / data-src) ---
    var allImgs = Array.from(root.querySelectorAll('img'));
    for (var i = 0; i < allImgs.length && result.length < 8; i++) {
        var el = allImgs[i];
        // Bo qua anh bi an
        var cs = window.getComputedStyle(el);
        if (cs.display === 'none' || cs.visibility === 'hidden') continue;
        // Bo qua nav/header/footer
        if (el.closest('nav') || el.closest('header') || el.closest('footer')) continue;
        // Bo qua avatar/profile icon
        var aria = (el.getAttribute('aria-label') || '').toLowerCase();
        if (aria.indexOf('avatar') >= 0 || aria.indexOf('profile') >= 0) continue;

        // Thu toan bo cac nguon URL - Facebook dung lazy load
        var src = el.currentSrc          // URL thuc sau khi load
            || el.src                    // src co the la placeholder
            || el.getAttribute('data-src')      // lazy load attribute
            || el.getAttribute('data-original') // fallback lazy attr
            || el.getAttribute('data-url')
            || '';
        if (!src) continue;

        var sz = getSize(el);
        addImg(src, sz[0], sz[1]);
    }

    // --- 2. Quet div[role="img"] voi background-image (Facebook dung cho post image trong dialog) ---
    var divImgs = Array.from(root.querySelectorAll('div[role="img"], i[role="img"], span[role="img"]'));
    for (var j = 0; j < divImgs.length && result.length < 8; j++) {
        var div = divImgs[j];
        var cs2 = window.getComputedStyle(div);
        if (cs2.display === 'none' || cs2.visibility === 'hidden') continue;
        var bg = cs2.backgroundImage || '';
        if (!bg || bg === 'none') continue;
        // Lay URL tu background-image: url("...")
        var match = bg.match(/url\\(["']?([^"')]+)["']?\\)/);
        if (!match) continue;
        var bgSrc = match[1];
        var rect3 = div.getBoundingClientRect();
        addImg(bgSrc, rect3.width || div.offsetWidth || 0, rect3.height || div.offsetHeight || 0);
    }

    // Sort theo dien tich lon nhat truoc
    result.sort(function(a, b) { return (b.w * b.h) - (a.w * a.h); });
    return result.slice(0, 6);
}
"""



# JS: Download ảnh qua browser (có auth cookie) — fallback khi screenshot thất bại
# Dùng mode: 'no-cors' để tránh CORS block, tuy nhiên response body bị giới hạn
# nên đây chỉ là fallback cuối cùng
_JS_FETCH_IMAGE_AS_BASE64 = """
async function(url) {
    try {
        // Thử same-origin fetch trước (hoạt động nếu ảnh cùng domain)
        var resp = await fetch(url, { credentials: 'include', mode: 'cors' });
        if (!resp.ok) return null;
        var blob = await resp.blob();
        return new Promise(function(resolve) {
            var reader = new FileReader();
            reader.onloadend = function() { resolve(reader.result); };
            reader.onerror = function() { resolve(null); };
            reader.readAsDataURL(blob);
        });
    } catch(e) {
        return null;
    }
}
"""

# JS: Tìm element img bằng src URL (trả về element hoặc null)
_JS_FIND_IMG_ELEMENT = """
(src) => {
    const imgs = Array.from(document.querySelectorAll('img'));
    // Khớp chính xác currentSrc hoặc src
    return imgs.find(img =>
        img.currentSrc === src || img.src === src
    ) || null;
}
"""

# JS: Tìm element div[role="img"] chứa background-image có URL tương ứng
_JS_FIND_DIV_IMG_ELEMENT = """
(srcFragment) => {
    const divs = Array.from(
        document.querySelectorAll('div[role="img"], span[role="img"], i[role="img"]')
    );
    return divs.find(div => {
        const bg = window.getComputedStyle(div).backgroundImage || '';
        return bg.indexOf(srcFragment) >= 0;
    }) || null;
}
"""
def _filter_cdn_images(raw_list: list) -> list[dict]:
    """Lọc chỉ giữ ảnh từ Facebook/Instagram CDN."""
    filtered = []
    for r in (raw_list or []):
        if not isinstance(r, dict):
            continue
        src = r.get("src", "")
        if not src or not src.startswith("https"):
            continue
        is_fb = (
            "fbcdn.net" in src
            or "scontent" in src
            or "cdninstagram" in src
            or "facebook.com" in src
            or "fbsbx.com" in src
        )
        if is_fb:
            filtered.append(r)
    return filtered[:3]


def extract_post_images(post_element: Any, page: Any = None) -> list[dict]:
    """Trích xuất thông tin ảnh trong bài viết.

    Chiến lược:
    1. Chạy JS trên post_element (scope hẹp)
    2. Nếu không tìm được và page được truyền vào → fallback sang toàn trang

    Trả về (filtered_images, total_raw_found):
    - filtered_images: list dict [{'src': url, 'w': int, 'h': int}]
    - total_raw_found: số ảnh thô tìm được trước filter CDN (dùng để debug)
    """
    total_raw = 0
    # --- Bước 1: thử post_element scope ---
    try:
        raw = post_element.evaluate(_JS_EXTRACT_POST_IMAGES)
        if isinstance(raw, list):
            total_raw = len(raw)
            filtered = _filter_cdn_images(raw)
            if filtered:
                return filtered, total_raw
    except Exception as exc:
        import sys
        print(f"[fb_scraper] extract_post_images (element scope) error: {exc}", file=sys.stderr)

    # --- Bước 2: fallback toàn trang (nếu ảnh nằm ngoài article) ---
    if page is not None:
        try:
            raw_page = page.evaluate(_JS_EXTRACT_POST_IMAGES)
            if isinstance(raw_page, list):
                total_raw = max(total_raw, len(raw_page))
                filtered_page = _filter_cdn_images(raw_page)
                if filtered_page:
                    return filtered_page, total_raw
        except Exception as exc:
            import sys
            print(f"[fb_scraper] extract_post_images (page scope) error: {exc}", file=sys.stderr)

    return [], total_raw


def download_post_images_as_base64(page: Any, image_infos: list[dict], max_images: int = 2) -> list[str]:
    """Download ảnh bài viết → base64 data URL.

    Chiến lược ưu tiên:
    1. Playwright element.screenshot() — chụp ảnh trực tiếp từ DOM render,
       KHÔNG qua CDN fetch → tránh hoàn toàn CORS/403 block của fbcdn.net
    2. Fallback: JS fetch() với credentials (nếu screenshot thất bại)

    Trả về list base64 data URL (data:image/png;base64,...).
    max_images: giới hạn số ảnh (mặc định 2) để tránh chậm.
    """
    import base64 as _b64
    import sys
    results: list[str] = []

    for info in image_infos[:max_images]:
        src = info.get("src", "")
        if not src:
            continue

        data_url: str | None = None

        # ─── Chiến lược 1: Playwright element.screenshot() ───────────────────
        # Chụp element trực tiếp từ browser render — không cần vượt CDN
        try:
            # Bước 1a: tìm <img> khớp src
            el_handle = page.evaluate_handle(_JS_FIND_IMG_ELEMENT, src)
            el = el_handle.as_element() if el_handle else None

            # Bước 1b: fallback tìm div[role="img"] với background-image
            if el is None:
                # Lấy phần cuối của path (bỏ query params) làm fragment tìm kiếm
                src_fragment = src.split("?")[0].rsplit("/", 1)[-1][:40]
                if src_fragment:
                    el_handle2 = page.evaluate_handle(_JS_FIND_DIV_IMG_ELEMENT, src_fragment)
                    el = el_handle2.as_element() if el_handle2 else None

            if el is not None:
                # Scroll element vào viewport nếu cần
                try:
                    el.scroll_into_view_if_needed(timeout=1500)
                    time.sleep(0.2)
                except Exception:
                    pass
                # Chụp screenshot của element (bytes PNG)
                screenshot_bytes = el.screenshot(timeout=4000)
                if screenshot_bytes and len(screenshot_bytes) > 500:
                    b64 = _b64.b64encode(screenshot_bytes).decode("utf-8")
                    data_url = f"data:image/png;base64,{b64}"
        except Exception as exc:
            print(f"[fb_scraper] screenshot error for {src[:60]}: {exc}", file=sys.stderr)

        # ─── Chiến lược 2: Fallback JS fetch() ───────────────────────────────
        if data_url is None:
            try:
                result = page.evaluate(_JS_FETCH_IMAGE_AS_BASE64, src)
                if result and isinstance(result, str) and result.startswith("data:image"):
                    data_url = result
            except Exception as exc:
                print(f"[fb_scraper] fetch fallback error for {src[:60]}: {exc}", file=sys.stderr)

        if data_url:
            results.append(data_url)

    return results

