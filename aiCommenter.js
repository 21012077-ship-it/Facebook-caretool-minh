const fs = require('node:fs/promises');
const path = require('node:path');

const DEFAULT_COMMENT_MODEL = process.env.OPENAI_COMMENT_MODEL || process.env.OPENAI_MODEL || 'gpt-4o-mini';
const DEFAULT_VISION_MODEL = process.env.OPENAI_VISION_MODEL || process.env.OPENAI_MODEL || 'gpt-4o-mini';
const OPENAI_BASE_URL = (process.env.OPENAI_BASE_URL || 'https://api.openai.com/v1').replace(/\/$/, '');

function requireApiKey() {
  const apiKey = process.env.OPENAI_API_KEY;
  if (!apiKey) {
    throw new Error('Thiếu OPENAI_API_KEY. Hãy export OPENAI_API_KEY trước khi chạy tool.');
  }
  return apiKey;
}

function compactText(value, maxLength = 3000) {
  return String(value || '')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, maxLength);
}

function buildCommentPrompt(postData) {
  return `Bạn là người dùng Facebook Việt Nam, bình luận theo kiểu Gen Z tự nhiên.

Nhiệm vụ: Đọc nội dung bài viết Facebook và nội dung chữ trong ảnh nếu có, sau đó viết 1 comment phù hợp nhất.

Yêu cầu:
- Bình luận phải giống người thật đang lướt Facebook rồi phản ứng ngay
- Bám sát nội dung bài, không comment chung chung
- Văn phong Gen Z Việt Nam
- Ngắn, thường từ 5 đến 18 từ
- Có thể dùng khẩu ngữ như: "nha", "á", "rồi đó", "có mùi", "căng", "trời ơi", "=)))", "😭"
- Không lạm dụng emoji
- Không viết quá trau chuốt, không văn mẫu, không giống AI
- Không lặp lại nguyên văn caption
- Ưu tiên kiểu comment phản ứng tự nhiên, hùa theo nội dung hoặc bắt đúng điểm gây cười
- Chỉ trả về đúng 1 câu comment, không giải thích, không thêm dấu ngoặc kép

Dữ liệu bài viết:
Page/account name: ${compactText(postData.accountName, 500) || '(không lấy được)'}
Post text: ${compactText(postData.postText, 3500) || '(không lấy được)'}
Hashtags: ${(postData.hashtags || []).join(', ') || '(không có)'}
Image/video thumbnail text nếu có: ${compactText(postData.imageText, 2500) || '(không lấy được)'}`;
}

async function callChatCompletion({ model, messages, maxTokens = 80, temperature = 0.85 }) {
  const apiKey = requireApiKey();
  const response = await fetch(`${OPENAI_BASE_URL}/chat/completions`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${apiKey}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      model,
      messages,
      temperature,
      max_tokens: maxTokens,
    }),
  });

  const rawBody = await response.text();
  let data;
  try {
    data = JSON.parse(rawBody);
  } catch (_) {
    data = { raw: rawBody };
  }

  if (!response.ok) {
    const detail = data?.error?.message || data?.raw || rawBody || response.statusText;
    throw new Error(`OpenAI API lỗi ${response.status}: ${detail}`);
  }

  return data?.choices?.[0]?.message?.content || '';
}

function sanitizeOneLineComment(text) {
  return compactText(text, 280)
    .replace(/^['"“”‘’]+|['"“”‘’]+$/g, '')
    .replace(/^Comment AI đề xuất:\s*/i, '')
    .trim();
}

async function generateComment(postData) {
  const prompt = buildCommentPrompt(postData);
  const content = await callChatCompletion({
    model: DEFAULT_COMMENT_MODEL,
    messages: [
      {
        role: 'system',
        content: 'Bạn chỉ viết đúng một câu bình luận Facebook tiếng Việt, không giải thích.',
      },
      { role: 'user', content: prompt },
    ],
  });

  const comment = sanitizeOneLineComment(content);
  if (!comment) {
    throw new Error('AI không trả về comment hợp lệ.');
  }
  return comment;
}

async function extractTextFromImages(imagePaths) {
  if (!imagePaths.length) {
    return '';
  }

  const content = [
    {
      type: 'text',
      text: 'Trích xuất ngắn gọn toàn bộ chữ tiếng Việt/tiếng Anh nhìn thấy trong các ảnh hoặc thumbnail video Facebook này. Nếu không thấy chữ, trả về chuỗi rỗng. Không mô tả cảnh vật.',
    },
  ];

  for (const imagePath of imagePaths) {
    const buffer = await fs.readFile(imagePath);
    const ext = path.extname(imagePath).toLowerCase();
    const mime = ext === '.png' ? 'image/png' : 'image/jpeg';
    content.push({
      type: 'image_url',
      image_url: { url: `data:${mime};base64,${buffer.toString('base64')}` },
    });
  }

  const result = await callChatCompletion({
    model: DEFAULT_VISION_MODEL,
    messages: [{ role: 'user', content }],
    maxTokens: 250,
    temperature: 0.1,
  });

  return compactText(result, 2500);
}

module.exports = {
  buildCommentPrompt,
  extractTextFromImages,
  generateComment,
};
