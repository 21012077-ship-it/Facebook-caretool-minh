#!/usr/bin/env node
const fs = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require('playwright');
const { extractTextFromImages, generateComment, requireApiKey } = require('./aiCommenter');

const POST_FLAG = '--post';
const DEFAULT_PROFILE_DIR = path.resolve(process.env.FB_PROFILE_DIR || 'fb_comment_profile');
const ENABLE_VISION = process.env.FB_COMMENT_ENABLE_VISION !== '0';

function log(message) {
  console.log(`[facebook-commenter] ${message}`);
}

function usage() {
  console.log(`Cách dùng:\n  node index.js "https://www.facebook.com/..."        # preview, không đăng\n  node index.js "https://www.facebook.com/..." --post # tự đăng comment\n\nBiến môi trường bắt buộc:\n  OPENAI_API_KEY=...\n\nTuỳ chọn:\n  FB_PROFILE_DIR=./fb_comment_profile\n  OPENAI_COMMENT_MODEL=gpt-4o-mini\n  OPENAI_VISION_MODEL=gpt-4o-mini\n  FB_COMMENT_ENABLE_VISION=0  # tắt OCR bằng AI vision`);
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
      log(`Đang OCR chữ trong ${imagePaths.length} ảnh/thumbnail bằng AI vision...`);
      visionText = await extractTextFromImages(imagePaths);
    }
  } catch (error) {
    log(`Không OCR được ảnh/thumbnail, tiếp tục bằng caption. Lý do: ${error.message}`);
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
  console.log(`Image text: ${postData.imageText || '(không có)'}`);
  console.log('========================');

  if (!postData.postText) {
    log('⚠️ Không lấy được caption rõ ràng trong article chính. AI sẽ tự quyết định dựa trên phần còn lại.');
  }
  return { postElement, postData };
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

async function main() {
  const parsed = parseArgs(process.argv);
  if (!parsed) return;

  const { url, shouldPost } = parsed;
  log(`Chế độ: ${shouldPost ? 'Auto-post' : 'Preview'}`);
  log(`Dùng Chromium profile cố định: ${DEFAULT_PROFILE_DIR}`);

  try {
    requireApiKey();
  } catch (error) {
    console.error(`❌ ${error.message}`);
    process.exitCode = 1;
    return;
  }

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

    log('🧠 Đang gửi ngữ cảnh bài viết vào AI để tạo comment...');
    let comment;
    try {
      comment = await generateComment(postData);
    } catch (error) {
      if (error.code === 'SKIP_COMMENT') {
        log('⚠️ AI trả về SKIP_COMMENT vì dữ liệu bài viết không đủ rõ, bỏ qua bài.');
        return;
      }
      if (error.reason === 'empty') {
        log('⚠️ AI trả về comment rỗng, bỏ qua bài.');
        return;
      }
      if (error.reason === 'too_long') {
        log('⚠️ Comment AI dài bất thường, bỏ qua bài.');
        return;
      }
      if (error.reason === 'generic') {
        log('Comment bị chặn vì quá chung chung');
        return;
      }
      throw error;
    }

    console.log(`💬 Comment AI đề xuất: ${comment}`);

    if (!shouldPost) {
      log('Preview mode: chỉ hiển thị comment để duyệt, không dán và không đăng.');
      log('Nếu muốn tự đăng, chạy thêm flag --post.');
      return;
    }

    await typeAndPost(page, postElement, comment);
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
  scrapePostContext,
};
