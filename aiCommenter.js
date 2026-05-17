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
  return `Bạn là một người trẻ Việt Nam thường xuyên lướt Facebook, biết bắt vibe bài đăng và để lại bình luận ngắn rất tự nhiên.

Nhiệm vụ:
Đọc nội dung bài viết, caption, hashtag và nội dung chữ trong ảnh nếu có, sau đó viết ra 1 bình luận phù hợp nhất với bài đó.

Phong cách bình luận:
- Kiểu Gen Z Việt Nam, tự nhiên, hơi đời, có cảm giác đang lướt thấy bài rồi comment ngay
- Có thể hài hước, hùa theo, cà khịa nhẹ, thả miếng, bắt đúng chi tiết đáng chú ý trong bài
- Ưu tiên comment khiến người đọc thấy “đúng ý bài ghê”
- Không bình luận như chatbot, không lịch sự quá mức, không văn mẫu

Yêu cầu bắt buộc:
- Chỉ viết 1 comment duy nhất
- Ngắn, thường từ 4 đến 16 từ
- Bám sát nội dung cụ thể của bài, không viết kiểu chung chung như “hay quá”, “đỉnh thật”, “xịn nha”
- Không lặp lại nguyên văn caption
- Không giải thích, không thêm dấu ngoặc kép
- Không cố nhồi trend nếu không hợp ngữ cảnh
- Không câu nào cũng phải có emoji
- Nếu dùng emoji thì chỉ 0–1 emoji là đủ

Có thể sử dụng tự nhiên các kiểu diễn đạt như:
- =)))
- 😭
- trời ơi
- có mùi rồi nha
- lộ quá rồi
- căng thế
- chịu luôn á
- ai mà chịu nổi
- nói vậy ai tin
- đúng bài này luôn
- nhìn là biết rồi
- không ổn nha
- cười kiểu này là dở rồi

Cách chọn hướng bình luận:
- Nếu bài hài/meme/phim: phản ứng vui, bắt đúng miếng gây cười
- Nếu bài có tình huống thả thính: comment kiểu trêu, hùa theo
- Nếu bài drama nhẹ: hóng hớt vừa phải, không công kích
- Nếu bài cảm xúc: đồng cảm ngắn gọn, tự nhiên
- Nếu bài khoe thành quả/sản phẩm: khen thật, không tâng bốc giả tạo

Dữ liệu bài viết:
- Người/page đăng: ${compactText(postData.accountName, 500) || '(không lấy được)'}
- Caption: ${compactText(postData.postText, 3500) || '(không lấy được)'}
- Hashtag: ${(postData.hashtags || []).join(', ') || '(không có)'}
- Nội dung chữ trong ảnh: ${compactText(postData.imageText, 2500) || '(không lấy được)'}

Hãy trả về đúng 1 bình luận phù hợp nhất.`;
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
