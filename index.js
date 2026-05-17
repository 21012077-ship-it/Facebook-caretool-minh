#!/usr/bin/env node
const fs = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require('playwright');
const { buildCommentPrompt, buildReplyPrompt, validateGeneratedComment } = require('./aiCommenter');

const POST_FLAG = '--post';
const DEFAULT_PROFILE_DIR = path.resolve(process.env.FB_PROFILE_DIR || 'fb_comment_profile');
const ENABLE_VISION = process.env.FB_COMMENT_ENABLE_VISION !== '0';

function log(message) {
  console.log(`[facebook-commenter] ${message}`);
}

function usage() {
  console.log(`Cách dùng:\n  node index.js "https://www.facebook.com/..."        # preview, không đăng\n  node index.js "https://www.facebook.com/..." --post # tự đăng comment\n\nLuồng mới không gọi OpenAI/Gemini API. Tool mở https://chatgpt.com bằng cùng Chromium profile để dùng cookie đăng nhập sẵn.\n\nTuỳ chọn:\n  FB_PROFILE_DIR=./fb_comment_profile\n  FB_COMMENT_ENABLE_VISION=0  # tắt chụp ảnh/thumbnail để gửi kèm ChatGPT`);
}

function parseArgs(argv) {
  const args = argv.slice(2);
  const shouldPost = args.includes(POST_FLAG);
  const url = args.find((arg) => !arg.startsWith('--'));
  if (!url) {
    usage();
    process.exitCode = 1;
    return null;
  }
  if (!/^https?:\/\/(www\.|m\.|mbasic\.)?facebook\.com\//i.test(url)) {
    throw new Error('URL không hợp lệ. Tool chỉ nhận link bắt đầu bằng https://www.facebook.com/...');
  }
  return { url, shouldPost };
}

async function humanDelay(min = 350, max = 1200) {
  const waitMs = Math.floor(min + Math.random() * (max - min));
  await new Promise((resolve) => setTimeout(resolve, waitMs));
}

async function ensureLoggedIn(context, page) {
  const cookies = await context.cookies('https://www.facebook.com');
  const hasLoginCookie = cookies.some((cookie) => cookie.name === 'c_user' && cookie.domain.includes('facebook.com'));
  if (hasLoginCookie) {
    return;
  }

  await page.goto('https://www.facebook.com/', { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForTimeout(3000);
  const loginFormVisible = await page.locator('input[name="email"], input[name="pass"]').first().isVisible().catch(() => false);
  if (loginFormVisible) {
    throw new Error(`Profile Chromium chưa đăng nhập Facebook. Hãy chạy tool một lần, đăng nhập trong cửa sổ mở ra, đóng tool rồi chạy lại. Profile đang dùng: ${DEFAULT_PROFILE_DIR}`);
  }
}

async function findMainPost(page) {
  await page.locator('div[role="article"], [role="main"]').first().waitFor({ state: 'visible', timeout: 25000 });

  const result = await page.evaluate(() => {
    const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
    const uiNoise = /(menu|facebook|meta ai|messenger|watch|reels|marketplace|bạn bè|friends|nhóm|groups|thước phim|saved|đã lưu|kỷ niệm|memories|công cụ chuyên nghiệp|professional dashboard|bảng feed|feed|trang chủ|home|thông báo|notifications)/i;
    const actionNoise = /^(thích|like|bình luận|comment|chia sẻ|share|gửi|send|phản hồi|reply|theo dõi|follow|tất cả cảm xúc|all reactions|xem thêm|see more|most relevant|phù hợp nhất)$/i;
    const isVisible = (element) => {
      if (!(element instanceof HTMLElement)) return false;
      const rect = element.getBoundingClientRect();
      const style = window.getComputedStyle(element);
      return rect.width > 40 && rect.height > 40 && style.visibility !== 'hidden' && style.display !== 'none';
    };
    const isChromeNode = (node) => Boolean(node.closest([
      'div[role="banner"]',
      'div[role="navigation"]',
      'div[role="complementary"]',
      'div[aria-label*="Menu" i]',
      'div[aria-label*="Facebook" i]',
      'nav',
      'header',
      'footer',
    ].join(',')));
    const collectOwnText = (root) => {
      const texts = [];
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
      while (walker.nextNode()) {
        const node = walker.currentNode;
        if (!(node instanceof HTMLElement) || !isVisible(node) || isChromeNode(node)) continue;
        const role = (node.getAttribute('role') || '').toLowerCase();
        const aria = normalize(node.getAttribute('aria-label') || '');
        if (['button', 'menuitem', 'navigation', 'banner', 'complementary'].includes(role)) continue;
        if (aria && (actionNoise.test(aria) || uiNoise.test(aria))) continue;
        const own = Array.from(node.childNodes)
          .filter((child) => child.nodeType === Node.TEXT_NODE)
          .map((child) => normalize(child.textContent))
          .filter(Boolean)
          .join(' ');
        if (own && !actionNoise.test(own) && !uiNoise.test(own)) texts.push(own);
      }
      return normalize(texts.join(' '));
    };
    const articles = Array.from(document.querySelectorAll('div[role="article"]')).filter((node) => isVisible(node) && !isChromeNode(node));
    const candidates = articles.map((article, index) => {
      const rect = article.getBoundingClientRect();
      const text = collectOwnText(article);
      const hasComposer = Boolean(article.querySelector('[contenteditable="true"][role="textbox"], div[aria-label*="comment" i], div[aria-label*="bình luận" i]'));
      const words = (text.match(/[A-Za-zÀ-ỹ0-9#]+/g) || []).length;
      const mediaCount = article.querySelectorAll('img, video').length;
      const chromePenalty = uiNoise.test(text) ? 200 : 0;
      const viewportBonus = rect.top > -250 && rect.top < window.innerHeight + 250 ? 100 : 0;
      const composerBonus = hasComposer ? 60 : 0;
      return { index, score: words * 8 + mediaCount * 20 + viewportBonus + composerBonus - chromePenalty, textLength: text.length, top: Math.abs(rect.top) };
    }).filter((candidate) => candidate.textLength > 0 || articles[candidate.index].querySelector('img, video'));
    candidates.sort((a, b) => (b.score - a.score) || (a.top - b.top));
    if (candidates[0]) return { type: 'article', index: candidates[0].index };
    return { type: 'main', index: 0 };
  });

  const locator = result.type === 'article'
    ? page.locator('div[role="article"]').nth(result.index)
    : page.locator('[role="main"], main').first();
  await locator.waitFor({ state: 'visible', timeout: 10000 });
  return locator;
}

async function expandPostText(page, postElement) {
  const labels = ['Xem thêm', 'See more', 'Show more', 'Thêm'];
  for (let round = 0; round < 3; round += 1) {
    let clicked = false;
    for (const label of labels) {
      const button = postElement.locator(`div[role="button"]:has-text("${label}"), span:has-text("${label}"), text=${label}`).first();
      if (await button.isVisible({ timeout: 700 }).catch(() => false)) {
        await humanDelay();
        await button.click({ timeout: 2000 }).catch(() => null);
        clicked = true;
        break;
      }
    }
    if (!clicked) break;
    await page.waitForTimeout(800);
  }
}

async function extractPostData(postElement) {
  return postElement.evaluate((root) => {
    const normalize = (text) => String(text || '').replace(/\s+/g, ' ').trim();
    const blockedExact = new Set([
      'Like', 'Comment', 'Share', 'Send', 'Reply', 'Follow', 'All reactions:', 'Most relevant',
      'Thích', 'Bình luận', 'Chia sẻ', 'Gửi', 'Phản hồi', 'Theo dõi', 'Tất cả cảm xúc', 'Phù hợp nhất',
      'Write a comment…', 'Write a comment...', 'Viết bình luận…', 'Viết bình luận...',
    ]);
    const blockedLine = /^(?:thích|like|bình luận|comment|chia sẻ|share|gửi|send|phản hồi|reply|theo dõi|follow|tất cả cảm xúc|all reactions|xem thêm|see more|ẩn bớt|see less|most relevant|phù hợp nhất)$/i;
    const timeLine = /^(?:\d+\s*(?:giây|phút|giờ|ngày|tuần|tháng|năm|s|m|h|d|w|mo|y)\s*(?:trước)?|vừa xong|just now)$/i;
    const uiNoise = /^(?:menu|facebook|meta ai|messenger|watch|reels|marketplace|bạn bè|friends|nhóm|groups|thước phim|saved|đã lưu|kỷ niệm|memories|công cụ chuyên nghiệp|professional dashboard|bảng feed|feed|trang chủ|home|thông báo|notifications)$/i;
    const isVisible = (element) => {
      if (!(element instanceof HTMLElement)) return false;
      const rect = element.getBoundingClientRect();
      const style = window.getComputedStyle(element);
      return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
    };
    const isChromeNode = (node) => Boolean(node.closest([
      'div[role="banner"]',
      'div[role="navigation"]',
      'div[role="complementary"]',
      'nav',
      'header',
      'footer',
    ].join(',')));
    const isNoise = (line) => {
      if (!line) return true;
      if (blockedExact.has(line) || blockedLine.test(line) || timeLine.test(line) || uiNoise.test(line)) return true;
      if (/^\d+[.,]?\d*\s*(k|m|n|tr)?\s*(thích|likes?|bình luận|comments?|shares?|lượt chia sẻ)$/i.test(line)) return true;
      if (/^https?:\/\//i.test(line)) return true;
      return false;
    };

    const accountCandidates = Array.from(root.querySelectorAll('h1 a, h2 a, h3 a, strong a, span a[role="link"], a[role="link"]'))
      .filter(isVisible)
      .map((node) => normalize(node.innerText || node.getAttribute('aria-label') || node.textContent || ''))
      .filter((text) => text && text.length <= 80 && !isNoise(text) && !/^#/.test(text));

    const lines = [];
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
    while (walker.nextNode()) {
      const node = walker.currentNode;
      if (!(node instanceof HTMLElement) || !isVisible(node) || isChromeNode(node)) continue;
      const role = (node.getAttribute('role') || '').toLowerCase();
      const aria = normalize(node.getAttribute('aria-label') || '');
      if (['button', 'menuitem', 'navigation', 'banner', 'complementary'].includes(role)) continue;
      if (aria && isNoise(aria)) continue;
      const ownText = Array.from(node.childNodes)
        .filter((child) => child.nodeType === Node.TEXT_NODE)
        .map((child) => normalize(child.textContent || ''))
        .filter(Boolean)
        .join(' ');
      if (!ownText) continue;
      for (const part of ownText.split(/\n+/).map(normalize)) {
        if (!isNoise(part) && !accountCandidates.includes(part) && !lines.includes(part)) {
          lines.push(part);
        }
      }
    }

    const joined = lines.join('\n');
    const hashtags = Array.from(new Set((joined.match(/#[\p{L}\p{N}_]+/gu) || []).slice(0, 20)));
    const postText = lines
      .filter((line) => !hashtags.includes(line))
      .join('\n')
      .slice(0, 5000);

    const domMediaText = Array.from(root.querySelectorAll('img, video, [aria-label], [title]'))
      .flatMap((node) => [node.getAttribute('alt'), node.getAttribute('aria-label'), node.getAttribute('title')])
      .map(normalize)
      .filter((text) => text && !/^Image may contain:/i.test(text) && !isNoise(text) && text.length > 2)
      .filter((text, index, array) => array.indexOf(text) === index)
      .slice(0, 12)
      .join('\n');

    return {
      accountName: accountCandidates[0] || '',
      postText,
      hashtags,
      domMediaText,
    };
  });
}

async function screenshotLikelyMedia(postElement) {
  if (!ENABLE_VISION) {
    return [];
  }

  const dir = path.resolve('.tmp_fb_comment_media');
  await fs.mkdir(dir, { recursive: true });
  const candidates = postElement.locator('img, video');
  const count = await candidates.count();
  const paths = [];

  for (let i = 0; i < count && paths.length < 4; i += 1) {
    const element = candidates.nth(i);
    const box = await element.boundingBox().catch(() => null);
    if (!box || box.width < 180 || box.height < 120) {
      continue;
    }
    const imagePath = path.join(dir, `media-${Date.now()}-${i}.png`);
    try {
      await element.screenshot({ path: imagePath, timeout: 5000 });
      paths.push(imagePath);
    } catch (_) {
      // Bỏ qua media không screenshot được.
    }
  }
  return paths;
}

async function scrapePostContext(page) {
  log('🔎 Đang quét nội dung bài Facebook...');
  const postElement = await findMainPost(page);
  await expandPostText(page, postElement);

  const data = await extractPostData(postElement);
  let visionText = '';
  let imagePaths = [];
  try {
    imagePaths = await screenshotLikelyMedia(postElement);
    if (imagePaths.length && ENABLE_VISION) {
      log(`Đã chụp ${imagePaths.length} ảnh/thumbnail để gửi kèm ChatGPT thủ công; không OCR bằng API.`);
    }
  } catch (error) {
    log(`Không chụp/gửi được ảnh thumbnail, tiếp tục bằng dữ liệu chữ đã quét. Lý do: ${error.message}`);
  }

  const postData = {
    accountName: data.accountName || '',
    postText: data.postText || '',
    hashtags: data.hashtags || [],
    imagePaths,
    imageText: [data.domMediaText, visionText].filter(Boolean).join('\n'),
  };

  console.log('===== POST CONTEXT =====');
  console.log(`Account: ${postData.accountName || '(không lấy được)'}`);
  console.log(`Post text: ${postData.postText || '(không lấy được)'}`);
  console.log(`Hashtags: ${postData.hashtags.join(', ') || '(không có)'}`);
  console.log(`Image text: ${postData.imageText || (postData.imagePaths.length ? '(chưa OCR / sẽ gửi ảnh kèm ChatGPT)' : '(không có)')}`);
  console.log('========================');

  if (!postData.postText) {
    log('⚠️ Không lấy được caption rõ ràng trong article chính. ChatGPT sẽ tự quyết định dựa trên phần còn lại.');
  }
  return { postElement, postData };
}

async function extractReplyCommentText(replyButton) {
  return replyButton.evaluate((button) => {
    const normalize = (text) => String(text || '').replace(/\s+/g, ' ').trim();
    const actionNoise = /^(?:thích|like|bình luận|comment|chia sẻ|share|gửi|send|phản hồi|reply|trả lời|xem thêm|see more|ẩn bớt|see less)$/i;
    const metaNoise = /^(?:\d+\s*(?:giây|phút|giờ|ngày|tuần|tháng|năm|s|m|h|d|w|mo|y)\s*(?:trước)?|vừa xong|just now|top fan|author)$/i;
    const isVisible = (element) => {
      if (!(element instanceof HTMLElement)) return false;
      const rect = element.getBoundingClientRect();
      const style = window.getComputedStyle(element);
      return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
    };
    const cleanLines = (text) => String(text || '')
      .split(/\n+/)
      .map(normalize)
      .filter((line) => line && line.length >= 2 && !actionNoise.test(line) && !metaNoise.test(line))
      .filter((line) => !/^\d+[.,]?\d*\s*(k|m|n|tr)?\s*(thích|likes?|phản hồi|repl(?:y|ies))$/i.test(line));

    let node = button.parentElement;
    let best = '';
    for (let depth = 0; node && depth < 8; depth += 1) {
      if (!isVisible(node)) {
        node = node.parentElement;
        continue;
      }
      const lines = cleanLines(node.innerText || node.textContent || '');
      const withoutButtonText = lines.filter((line) => !/^(phản hồi|reply|trả lời)$/i.test(line));
      const candidate = withoutButtonText
        .filter((line) => !/^(thích|like)$/i.test(line))
        .sort((a, b) => b.length - a.length)[0] || '';
      if (candidate.length > best.length) best = candidate;
      if (best.length >= 12 && best.length <= 500) break;
      node = node.parentElement;
    }
    return best.slice(0, 1200);
  });
}

async function extractFirstCommentText(commentElement) {
  return commentElement.evaluate((root) => {
    const normalize = (text) => String(text || '').replace(/\s+/g, ' ').trim();
    const actionNoise = /^(?:thích|like|bình luận|comment|chia sẻ|share|gửi|send|phản hồi|reply|trả lời|xem thêm|see more|ẩn bớt|see less)$/i;
    const metaNoise = /^(?:\d+\s*(?:giây|phút|giờ|ngày|tuần|tháng|năm|s|m|h|d|w|mo|y)\s*(?:trước)?|vừa xong|just now|top fan|author)$/i;
    const isVisible = (element) => {
      if (!(element instanceof HTMLElement)) return false;
      const rect = element.getBoundingClientRect();
      const style = window.getComputedStyle(element);
      return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
    };
    const cleanLines = (text) => String(text || '')
      .split(/\n+/)
      .map(normalize)
      .filter((line) => line && line.length >= 2 && !actionNoise.test(line) && !metaNoise.test(line))
      .filter((line) => !/^\d+[.,]?\d*\s*(k|m|n|tr)?\s*(thích|likes?|phản hồi|repl(?:y|ies)|bình luận|comments?)$/i.test(line));

    const textNodes = Array.from(root.querySelectorAll('div[dir="auto"], span[dir="auto"]'))
      .filter((element) => isVisible(element) && !element.closest('[role="button"]'))
      .flatMap((element) => cleanLines(element.innerText || element.textContent || ''));
    const lines = textNodes.length ? textNodes : cleanLines(root.innerText || root.textContent || '');
    const candidate = lines
      .filter((line) => !/^(phản hồi|reply|trả lời|thích|like)$/i.test(line))
      .sort((a, b) => b.length - a.length)[0] || '';
    return candidate.slice(0, 1200);
  });
}

async function findFirstVisibleComment(postElement) {
  const selectors = [
    'div[aria-label*="Comment by" i]',
    'div[aria-label*="Bình luận của" i]',
    'div[role="article"]',
  ];
  const seen = new Set();

  for (const selector of selectors) {
    const comments = postElement.locator(selector);
    const count = await comments.count().catch(() => 0);
    for (let i = 0; i < Math.min(count, 20); i += 1) {
      const container = comments.nth(i);
      if (!(await container.isVisible({ timeout: 500 }).catch(() => false))) continue;
      const key = await container.evaluate((node) => {
        const rect = node.getBoundingClientRect();
        return `${Math.round(rect.top)}:${Math.round(rect.left)}:${Math.round(rect.width)}:${(node.textContent || '').trim().slice(0, 80)}`;
      }).catch(() => `${selector}:${i}`);
      if (seen.has(key)) continue;
      seen.add(key);

      const commentText = sanitizeScannedCommentText(await extractFirstCommentText(container).catch(() => ''));
      if (!commentText || commentText.length < 2) continue;
      return { container, commentText, isFirstCommentFallback: true };
    }
  }

  return null;
}

async function clickReplyTarget(page, postElement, target) {
  if (target.button) {
    await target.button.scrollIntoViewIfNeeded().catch(() => null);
    await humanDelay(500, 1200);
    await target.button.click({ timeout: 5000 });
    return;
  }

  if (!target.container) {
    throw new Error('Không tìm thấy comment đầu tiên để phản hồi.');
  }

  await target.container.scrollIntoViewIfNeeded().catch(() => null);
  await humanDelay(500, 1200);
  await target.container.hover({ timeout: 3000 }).catch(() => null);
  await page.waitForTimeout(700);

  const replySelectors = [
    'div[role="button"]:has-text("Phản hồi")',
    'div[role="button"]:has-text("Reply")',
    'span:has-text("Phản hồi")',
    'span:has-text("Reply")',
    'text=Phản hồi',
    'text=Reply',
  ];

  for (const selector of replySelectors) {
    const button = target.container.locator(selector).first();
    if (await button.isVisible({ timeout: 1000 }).catch(() => false)) {
      await button.click({ timeout: 5000 });
      return;
    }
  }

  for (const selector of replySelectors) {
    const button = postElement.locator(selector).first();
    if (await button.isVisible({ timeout: 1000 }).catch(() => false)) {
      await button.click({ timeout: 5000 });
      return;
    }
  }

  throw new Error('Đã tìm thấy comment đầu tiên nhưng không hiện nút Phản hồi/Reply để bấm.');
}

async function findReplyTargetComment(postElement) {
  const selectors = [
    'div[role="button"]:has-text("Phản hồi")',
    'div[role="button"]:has-text("Reply")',
    'span:has-text("Phản hồi")',
    'span:has-text("Reply")',
    'text=Phản hồi',
    'text=Reply',
  ];
  const seen = new Set();
  const candidates = [];

  for (const selector of selectors) {
    const buttons = postElement.locator(selector);
    const count = await buttons.count().catch(() => 0);
    for (let i = 0; i < Math.min(count, 12); i += 1) {
      const button = buttons.nth(i);
      if (!(await button.isVisible({ timeout: 500 }).catch(() => false))) continue;
      const key = await button.evaluate((node) => {
        const rect = node.getBoundingClientRect();
        return `${Math.round(rect.top)}:${Math.round(rect.left)}:${(node.textContent || '').trim()}`;
      }).catch(() => `${selector}:${i}`);
      if (seen.has(key)) continue;
      seen.add(key);
      const commentText = sanitizeScannedCommentText(await extractReplyCommentText(button).catch(() => ''));
      if (!commentText) continue;
      candidates.push({ button, commentText });
    }
  }

  return candidates[0] || null;
}

function sanitizeScannedCommentText(text) {
  return String(text || '')
    .replace(/\s+/g, ' ')
    .replace(/^(?:Fan cứng|Top fan|Author)\s+/i, '')
    .trim()
    .slice(0, 1200);
}

async function scanReplyTargetComment(page, postElement) {
  log('🔎 Bắt đầu quét comment cần trả lời...');
  for (let round = 0; round < 5; round += 1) {
    const target = await findReplyTargetComment(postElement);
    if (target) {
      log(`💬 Đã quét comment cần trả lời: ${target.commentText.slice(0, 140)}${target.commentText.length > 140 ? '...' : ''}`);
      return target.commentText;
    }
    await page.mouse.wheel(0, 650).catch(() => null);
    await page.waitForTimeout(900);
  }
  const firstComment = await findFirstVisibleComment(postElement);
  if (firstComment) {
    log(`💬 Không thấy comment nào có nút Phản hồi/Reply; dùng comment đầu tiên: ${firstComment.commentText.slice(0, 140)}${firstComment.commentText.length > 140 ? '...' : ''}`);
    return firstComment.commentText;
  }

  log('⚠️ Không quét được comment nào để trả lời.');
  return '';
}

async function findReplyBox(page, postElement) {
  const target = await findReplyTargetComment(postElement) || await findFirstVisibleComment(postElement);
  if (!target) {
    throw new Error('Không tìm thấy comment để trả lời.');
  }

  if (target.isFirstCommentFallback) {
    log('↪️ Không có nút Reply sẵn; thử phản hồi ở comment đầu tiên.');
  }
  await clickReplyTarget(page, postElement, target);
  await page.waitForTimeout(1000);

  const selectors = [
    '[contenteditable="true"][role="textbox"][aria-label*="reply" i]',
    '[contenteditable="true"][role="textbox"][aria-label*="phản hồi" i]',
    '[contenteditable="true"][role="textbox"][aria-label*="trả lời" i]',
    '[contenteditable="true"][aria-label*="reply" i]',
    '[contenteditable="true"][aria-label*="phản hồi" i]',
    'div[role="textbox"][contenteditable="true"][data-lexical-editor="true"]',
  ];

  for (const selector of selectors) {
    const box = postElement.locator(selector).last();
    if (await box.isVisible({ timeout: 2000 }).catch(() => false)) return box;
  }
  throw new Error('Không tìm thấy ô nhập phản hồi sau khi bấm Reply.');
}

async function findCommentBox(page, postElement) {
  const selectors = [
    '[contenteditable="true"][role="textbox"][aria-label*="comment" i]:not([aria-label*="reply" i])',
    '[contenteditable="true"][role="textbox"][aria-label*="bình luận" i]:not([aria-label*="phản hồi" i])',
    '[contenteditable="true"][aria-label*="comment" i]:not([aria-label*="reply" i])',
    '[contenteditable="true"][aria-label*="bình luận" i]:not([aria-label*="phản hồi" i])',
    'div[role="textbox"][contenteditable="true"][data-lexical-editor="true"]',
  ];

  const clickTargets = [
    'div[role="button"][aria-label="Bình luận"]',
    'div[role="button"][aria-label="Comment"]',
    'span:has-text("Bình luận")',
    'span:has-text("Comment")',
  ];

  for (const selector of selectors) {
    const box = postElement.locator(selector).first();
    if (await box.isVisible({ timeout: 1200 }).catch(() => false)) {
      const aria = (await box.getAttribute('aria-label').catch(() => '') || '').toLowerCase();
      if (!/reply|phản hồi|trả lời/.test(aria)) return box;
    }
  }

  for (const target of clickTargets) {
    const button = postElement.locator(target).first();
    if (await button.isVisible({ timeout: 1000 }).catch(() => false)) {
      const label = (await button.textContent().catch(() => '') || '') + ' ' + (await button.getAttribute('aria-label').catch(() => '') || '');
      if (/phản hồi|reply|trả lời/i.test(label)) continue;
      await button.scrollIntoViewIfNeeded().catch(() => null);
      await humanDelay();
      await button.click({ timeout: 3000 }).catch(() => null);
      await page.waitForTimeout(1000);
      break;
    }
  }

  for (const selector of selectors) {
    const boxes = postElement.locator(selector);
    const count = await boxes.count().catch(() => 0);
    for (let i = 0; i < Math.min(count, 6); i += 1) {
      const box = boxes.nth(i);
      if (!(await box.isVisible({ timeout: 800 }).catch(() => false))) continue;
      const aria = (await box.getAttribute('aria-label').catch(() => '') || '').toLowerCase();
      if (!/reply|phản hồi|trả lời/.test(aria)) return box;
    }
  }

  throw new Error('Không tìm thấy ô bình luận chính của bài post, bỏ qua link.');
}

async function typeAndPost(page, postElement, comment) {
  log('✍️ Đang nhập comment vào ô bình luận bài post...');
  const box = await findCommentBox(page, postElement);
  await humanDelay(500, 1400);
  await box.click({ timeout: 10000 });
  await humanDelay(300, 900);
  await box.fill('').catch(() => null);
  await page.keyboard.type(comment, { delay: Math.floor(35 + Math.random() * 95) });
  await humanDelay(700, 1800);
  await page.keyboard.press('Enter');
  await page.waitForTimeout(2500);
  log('✅ Đã đăng comment vào bài post thành công.');
}



async function typeAndReply(page, postElement, comment) {
  log('✍️ Đang nhập reply vào comment đã quét...');
  const box = await findReplyBox(page, postElement);
  await humanDelay(500, 1400);
  await box.click({ timeout: 10000 });
  await humanDelay(300, 900);
  await box.fill('').catch(() => null);
  await page.keyboard.type(comment, { delay: Math.floor(35 + Math.random() * 95) });
  await humanDelay(700, 1800);
  await page.keyboard.press('Enter');
  await page.waitForTimeout(2500);
  log('✅ Đã đăng reply vào comment thành công.');
}

async function findChatGPTComposer(page) {
  const selectors = [
    '#prompt-textarea',
    'div[contenteditable="true"][id="prompt-textarea"]',
    'div[contenteditable="true"][data-testid="prompt-textarea"]',
    'textarea[data-testid="prompt-textarea"]',
    'textarea[placeholder*="Message" i]',
    'textarea[placeholder*="Nhắn" i]',
    'div[contenteditable="true"]',
  ];

  for (const selector of selectors) {
    const composer = page.locator(selector).last();
    if (await composer.isVisible({ timeout: 2500 }).catch(() => false)) {
      return composer;
    }
  }
  throw new Error('Không tìm thấy ô nhập ChatGPT. Hãy đăng nhập ChatGPT trong profile Chromium rồi chạy lại.');
}

async function ensureChatGPTLoggedIn(page) {
  await page.goto('https://chatgpt.com/', { waitUntil: 'domcontentloaded', timeout: 90000 });
  await page.waitForLoadState('networkidle', { timeout: 25000 }).catch(() => null);
  await page.waitForTimeout(3000);

  const loginVisible = await page.locator('text=/log in|đăng nhập|sign up|đăng ký/i').first().isVisible({ timeout: 2000 }).catch(() => false);
  const composerVisible = await page.locator('#prompt-textarea, textarea[data-testid="prompt-textarea"], div[contenteditable="true"]').last().isVisible({ timeout: 3000 }).catch(() => false);
  if (loginVisible && !composerVisible) {
    throw new Error(`Chromium profile chưa đăng nhập ChatGPT. Hãy mở profile ${DEFAULT_PROFILE_DIR}, đăng nhập https://chatgpt.com một lần để lưu cookie rồi chạy lại.`);
  }
}

async function attachImagesToChatGPT(chatPage, imagePaths) {
  if (!imagePaths.length) return;

  const existingInputs = await chatPage.locator('input[type="file"]').count().catch(() => 0);
  if (!existingInputs) {
    const attachButton = chatPage.locator('[aria-label*="Attach" i], [aria-label*="Tải" i], [aria-label*="Đính" i], button:has-text("+")').first();
    if (await attachButton.isVisible({ timeout: 1500 }).catch(() => false)) {
      await attachButton.click({ timeout: 3000 }).catch(() => null);
      await chatPage.waitForTimeout(700);
    }
  }

  const fileInput = chatPage.locator('input[type="file"]').last();
  if (await fileInput.count().catch(() => 0)) {
    await fileInput.setInputFiles(imagePaths, { timeout: 15000 });
    log(`📎 Đã đính kèm ${imagePaths.length} ảnh/thumbnail vào ChatGPT.`);
    await chatPage.waitForTimeout(2500);
  } else {
    log('⚠️ Không tìm thấy nút/file input để đính kèm ảnh vào ChatGPT; prompt vẫn ghi rõ ảnh chưa OCR.');
  }
}

async function submitChatGPTPrompt(chatPage, prompt) {
  const composer = await findChatGPTComposer(chatPage);
  await composer.click({ timeout: 10000 });
  await humanDelay(300, 800);
  await chatPage.keyboard.press(process.platform === 'darwin' ? 'Meta+A' : 'Control+A').catch(() => null);
  await chatPage.keyboard.insertText(prompt);
  await humanDelay(500, 1200);

  const sendButton = chatPage.locator('[data-testid="send-button"], button[aria-label*="Send" i], button[aria-label*="Gửi" i]').last();
  if (await sendButton.isVisible({ timeout: 2500 }).catch(() => false)) {
    await sendButton.click({ timeout: 10000 });
  } else {
    await chatPage.keyboard.press('Enter');
  }
}

async function waitForChatGPTResponse(chatPage, beforeCount) {
  log('⏳ Đang chờ ChatGPT trả comment trên web...');
  const assistantSelector = '[data-message-author-role="assistant"], div.markdown.prose, .markdown';
  await chatPage.waitForFunction(
    ({ selector, count }) => document.querySelectorAll(selector).length > count,
    { selector: assistantSelector, count: beforeCount },
    { timeout: 180000 },
  ).catch(() => null);

  await chatPage.waitForFunction(() => {
    const stopButton = document.querySelector('[data-testid="stop-button"], button[aria-label*="Stop" i], button[aria-label*="Dừng" i]');
    return !stopButton;
  }, null, { timeout: 180000 }).catch(() => null);
  await chatPage.waitForTimeout(1500);

  const responses = await chatPage.locator(assistantSelector).evaluateAll((nodes) => nodes.map((node) => (node.innerText || node.textContent || '').trim()).filter(Boolean));
  return responses[responses.length - 1] || '';
}

async function generateCommentWithChatGPT(context, postData, targetComment = '') {
  const prompt = targetComment ? buildReplyPrompt(postData, targetComment) : buildCommentPrompt(postData);
  const chatPage = await context.newPage();
  chatPage.setDefaultTimeout(20000);

  try {
    log('🧠 Mở ChatGPT thủ công trên máy bằng cookie/profile Chromium...');
    await ensureChatGPTLoggedIn(chatPage);

    const assistantSelector = '[data-message-author-role="assistant"], div.markdown.prose, .markdown';
    const beforeCount = await chatPage.locator(assistantSelector).count().catch(() => 0);

    await attachImagesToChatGPT(chatPage, postData.imagePaths || []);
    log('📋 Đang paste prompt + dữ liệu bài viết và comment đã quét vào ChatGPT...');
    await submitChatGPTPrompt(chatPage, prompt);

    const rawComment = await waitForChatGPTResponse(chatPage, beforeCount);
    const validation = validateGeneratedComment(rawComment);
    if (!validation.ok) {
      if (validation.reason === 'skip') {
        log('⚠️ ChatGPT trả về SKIP_COMMENT vì dữ liệu bài viết không đủ rõ, bỏ qua bài.');
        return '';
      }
      log(`⚠️ Comment ChatGPT không hợp lệ (${validation.reason}): ${validation.comment || '(rỗng)'}`);
      return '';
    }
    return validation.comment;
  } finally {
    await chatPage.close().catch(() => null);
  }
}

async function main() {
  const parsed = parseArgs(process.argv);
  if (!parsed) return;

  const { url, shouldPost } = parsed;
  log(`Chế độ: ${shouldPost ? 'Auto-post' : 'Preview'}`);
  log(`Dùng Chromium profile cố định: ${DEFAULT_PROFILE_DIR}`);

  const context = await chromium.launchPersistentContext(DEFAULT_PROFILE_DIR, {
    channel: process.env.PLAYWRIGHT_CHROMIUM_CHANNEL || undefined,
    headless: false,
    viewport: { width: 1280, height: 900 },
    locale: 'vi-VN',
    timezoneId: 'Asia/Ho_Chi_Minh',
    slowMo: 80,
    args: [
      '--disable-blink-features=AutomationControlled',
      '--disable-features=Translate',
      '--disable-dev-shm-usage',
    ],
  });

  try {
    const page = context.pages()[0] || await context.newPage();
    page.setDefaultTimeout(20000);

    log('Kiểm tra trạng thái đăng nhập Facebook...');
    await ensureLoggedIn(context, page);

    log('Mở link bài viết...');
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 90000 });
    await page.waitForLoadState('networkidle', { timeout: 25000 }).catch(() => null);
    await page.waitForTimeout(3000);

    const { postElement, postData } = await scrapePostContext(page);
    const targetComment = await scanReplyTargetComment(page, postElement);
    if (!targetComment) {
      return;
    }

    const comment = await generateCommentWithChatGPT(context, postData, targetComment);
    if (!comment) {
      return;
    }

    console.log(`💬 Reply ChatGPT đề xuất: ${comment}`);

    if (!shouldPost) {
      log('Preview mode: chỉ hiển thị reply để duyệt, không dán và không đăng.');
      log('Nếu muốn tự đăng, chạy thêm flag --post.');
      return;
    }

    await typeAndReply(page, postElement, comment);
  } finally {
    await context.close().catch(() => null);
  }
}

main().catch((error) => {
  console.error(`\nLỗi: ${error.message}`);
  process.exitCode = 1;
});

module.exports = {
  extractPostData,
  findMainPost,
  extractReplyCommentText,
  findReplyTargetComment,
  findFirstVisibleComment,
  scanReplyTargetComment,
  scrapePostContext,
};
