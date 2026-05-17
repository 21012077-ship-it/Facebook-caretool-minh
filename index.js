#!/usr/bin/env node
const fs = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require('playwright');
const { extractTextFromImages, generateComment } = require('./aiCommenter');

const POST_FLAG = '--post';
const DEFAULT_PROFILE_DIR = path.resolve(process.env.FB_PROFILE_DIR || 'fb_comment_profile');
const ENABLE_VISION = process.env.FB_COMMENT_ENABLE_VISION !== '0';

function log(message) {
  console.log(`[facebook-commenter] ${message}`);
}

function usage() {
  console.log(`Cách dùng:\n  node index.js "https://www.facebook.com/..."        # preview, không đăng\n  node index.js "https://www.facebook.com/..." --post # tự đăng comment\n\nBiến môi trường cần có:\n  OPENAI_API_KEY=...\n\nTuỳ chọn:\n  FB_PROFILE_DIR=./fb_comment_profile\n  OPENAI_MODEL=gpt-4o-mini\n  FB_COMMENT_ENABLE_VISION=0  # tắt OCR bằng AI vision`);
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

async function safeClickByText(page, patterns, timeout = 2500) {
  for (const pattern of patterns) {
    try {
      const locator = page.getByText(pattern, { exact: false }).first();
      if (await locator.isVisible({ timeout })) {
        await humanDelay();
        await locator.click({ timeout });
        return true;
      }
    } catch (_) {
      // Facebook thay đổi DOM thường xuyên; thử selector kế tiếp.
    }
  }
  return false;
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

async function expandPostText(page) {
  const labels = ['Xem thêm', 'See more', 'Show more', 'Thêm'];
  for (let round = 0; round < 3; round += 1) {
    const clicked = await safeClickByText(page, labels, 800);
    if (!clicked) break;
    await page.waitForTimeout(800);
  }
}

async function findMainPost(page) {
  const article = page.locator('div[role="article"]').first();
  if (await article.count()) {
    await article.waitFor({ state: 'visible', timeout: 20000 });
    return article;
  }

  const main = page.locator('main').first();
  if (await main.count()) {
    await main.waitFor({ state: 'visible', timeout: 20000 });
    return main;
  }

  throw new Error('Không tìm thấy vùng chứa bài viết chính trên Facebook.');
}

async function extractPostData(postLocator) {
  return postLocator.evaluate((root) => {
    const normalize = (text) => (text || '').replace(/\s+/g, ' ').trim();
    const blockedPhrases = new Set([
      'Like', 'Comment', 'Share', 'Thích', 'Bình luận', 'Chia sẻ', 'Send', 'Gửi',
      'All reactions:', 'Most relevant', 'Phù hợp nhất', 'Write a comment…', 'Viết bình luận…',
    ]);

    const rawVisibleText = root.innerText || '';
    const allVisibleText = normalize(rawVisibleText);
    const lines = rawVisibleText
      .split(/\n+/)
      .map(normalize)
      .filter((line) => line && !blockedPhrases.has(line))
      .filter((line) => !/^\d+[wdhms]$/.test(line));

    const accountCandidates = Array.from(root.querySelectorAll('h1, h2, h3, strong a, span a'))
      .map((node) => normalize(node.innerText || node.getAttribute('aria-label') || ''))
      .filter(Boolean)
      .filter((text) => text.length <= 80);

    const mediaText = Array.from(root.querySelectorAll('img, video, [aria-label], [title]'))
      .flatMap((node) => [node.getAttribute('alt'), node.getAttribute('aria-label'), node.getAttribute('title')])
      .map(normalize)
      .filter(Boolean)
      .filter((text) => !/^Image may contain:/i.test(text) && !blockedPhrases.has(text));

    const hashtags = Array.from(new Set((allVisibleText.match(/#[\p{L}\p{N}_]+/gu) || []).slice(0, 20)));
    const postText = lines
      .filter((line) => !accountCandidates.includes(line))
      .filter((line) => !line.startsWith('http'))
      .join('\n')
      .slice(0, 5000);

    return {
      accountName: accountCandidates[0] || '',
      postText,
      hashtags,
      domMediaText: Array.from(new Set(mediaText)).slice(0, 12).join('\n'),
    };
  });
}

async function screenshotLikelyMedia(postLocator) {
  if (!ENABLE_VISION || !process.env.OPENAI_API_KEY) {
    return [];
  }

  const dir = path.resolve('.tmp_fb_comment_media');
  await fs.mkdir(dir, { recursive: true });
  const candidates = postLocator.locator('img, video');
  const count = Math.min(await candidates.count(), 4);
  const paths = [];

  for (let i = 0; i < count; i += 1) {
    const element = candidates.nth(i);
    const box = await element.boundingBox().catch(() => null);
    if (!box || box.width < 120 || box.height < 80) {
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

async function getImageText(postLocator) {
  const data = await extractPostData(postLocator);
  let visionText = '';
  try {
    const imagePaths = await screenshotLikelyMedia(postLocator);
    if (imagePaths.length) {
      log(`Đang OCR chữ trong ${imagePaths.length} ảnh/thumbnail bằng AI vision...`);
      visionText = await extractTextFromImages(imagePaths);
    }
  } catch (error) {
    log(`Không OCR được ảnh/thumbnail, tiếp tục bằng caption. Lý do: ${error.message}`);
  }
  return { ...data, imageText: [data.domMediaText, visionText].filter(Boolean).join('\n') };
}

async function findCommentBox(page, postLocator) {
  const selectors = [
    '[contenteditable="true"][role="textbox"][aria-label*="comment" i]',
    '[contenteditable="true"][role="textbox"][aria-label*="bình luận" i]',
    '[contenteditable="true"][aria-label*="comment" i]',
    '[contenteditable="true"][aria-label*="bình luận" i]',
    'div[role="textbox"][contenteditable="true"]',
  ];

  await safeClickByText(page, [/Bình luận/i, /Comment/i], 1000).catch(() => false);
  await humanDelay();

  for (const selector of selectors) {
    const inPost = postLocator.locator(selector).first();
    if (await inPost.isVisible({ timeout: 1500 }).catch(() => false)) {
      return inPost;
    }
    const onPage = page.locator(selector).first();
    if (await onPage.isVisible({ timeout: 1500 }).catch(() => false)) {
      return onPage;
    }
  }
  throw new Error('Không tìm thấy ô comment của bài viết.');
}

async function typeAndPost(page, postLocator, comment) {
  const box = await findCommentBox(page, postLocator);
  await humanDelay(500, 1400);
  await box.click({ timeout: 10000 });
  await humanDelay(300, 900);
  await box.fill('').catch(() => null);
  await page.keyboard.type(comment, { delay: Math.floor(35 + Math.random() * 95) });
  await humanDelay(700, 1800);
  await page.keyboard.press('Enter');
  await page.waitForTimeout(2500);
  log('Đã đăng comment thành công.');
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

    log('Tìm vùng chứa post chính...');
    const postLocator = await findMainPost(page);
    await expandPostText(page);

    log('Quét caption, page/account, hashtag và chữ trong media nếu có...');
    const postData = await getImageText(postLocator);
    if (!postData.postText && !postData.imageText) {
      throw new Error('Không lấy được caption hoặc chữ trong ảnh/video thumbnail của bài viết.');
    }

    log(`Page/account: ${postData.accountName || '(không lấy được)'}`);
    log(`Caption length: ${postData.postText.length} ký tự; Image text length: ${postData.imageText.length} ký tự`);

    log('Gửi dữ liệu bài viết vào AI để tạo comment...');
    const comment = await generateComment(postData);
    console.log('\nComment AI đề xuất:');
    console.log(`"${comment}"\n`);

    if (!shouldPost) {
      log('Preview mode: chỉ hiển thị comment để duyệt, không dán và không đăng.');
      log('Nếu muốn tự đăng, chạy thêm flag --post.');
      return;
    }

    await typeAndPost(page, postLocator, comment);
  } finally {
    await context.close().catch(() => null);
  }
}

main().catch((error) => {
  console.error(`\nLỗi: ${error.message}`);
  process.exitCode = 1;
});
