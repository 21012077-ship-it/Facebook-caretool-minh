const fs = require('node:fs/promises');
const path = require('node:path');

const DEFAULT_COMMENT_MODEL = process.env.OPENAI_COMMENT_MODEL || process.env.OPENAI_MODEL || 'gpt-4o-mini';
const DEFAULT_VISION_MODEL = process.env.OPENAI_VISION_MODEL || process.env.OPENAI_MODEL || 'gpt-4o-mini';
const OPENAI_BASE_URL = (process.env.OPENAI_BASE_URL || 'https://api.openai.com/v1').replace(/\/$/, '');
const SKIP_COMMENT = 'SKIP_COMMENT';

const BANNED_COMMENT_PATTERNS = [
  /mình nghĩ nên/i,
  /từng tình huống/i,
  /mỗi người có thể/i,
  /góc nhìn khác nhau/i,
  /đáng suy ngẫm/i,
  /vấn đề thú vị/i,
  /rất đồng tình/i,
];

function requireApiKey() {
  const apiKey = process.env.OPENAI_API_KEY;
  if (!apiKey) {
    const error = new Error('Thiếu OPENAI_API_KEY, bỏ qua link vì không thể tạo comment bằng AI.');
    error.code = 'OPENAI_API_KEY_MISSING';
    throw error;
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
  return `Bạn là một người trẻ Việt Nam thường xuyên lướt Facebook và bình luận rất tự nhiên.

Nhiệm vụ:
Đọc kỹ toàn bộ ngữ cảnh của bài đăng Facebook, bao gồm:
- Tên page hoặc tài khoản đăng bài
- Nội dung caption/post text
- Hashtag
- Chữ trong ảnh hoặc thumbnail video nếu có

Sau đó viết ra đúng 1 bình luận phù hợp nhất với bài đăng.

Phong cách bình luận mong muốn:
- Kiểu Gen Z Việt Nam, tự nhiên, hơi đời
- Giống người thật lướt thấy bài rồi comment ngay
- Bám rất sát nội dung cụ thể của bài
- Có thể hùa theo, thả miếng, trêu nhẹ, cà khịa nhẹ, bắt đúng tình huống gây cười hoặc chi tiết nổi bật
- Ưu tiên những câu khiến người đọc thấy “comment này đúng bài ghê”
- Không viết như chatbot
- Không văn mẫu
- Không nghị luận dài dòng

Yêu cầu bắt buộc:
- Chỉ trả về đúng 1 comment duy nhất
- Không giải thích
- Không thêm dấu ngoặc kép
- Không thêm tiền tố như “Comment:”
- Độ dài ưu tiên khoảng 4 đến 16 từ
- Có thể dài hơn một chút nếu thật sự cần, nhưng không được lan man
- Không lặp lại nguyên văn caption
- Không viết comment chung chung không liên quan
- Không dùng kiểu giọng AI như:
  + "mình nghĩ nên nhìn theo từng tình huống thực tế"
  + "mỗi người có thể có một góc nhìn khác nhau"
  + "nội dung này rất đáng suy ngẫm"
  + "đây là một vấn đề thú vị"
  + "rất đồng tình với quan điểm này"
- Không dùng các lời khen rỗng như:
  + "hay quá"
  + "đỉnh thật"
  + "tuyệt vời"
  + "xịn nha"
  nếu không thực sự hợp ngữ cảnh
- Không lạm dụng emoji
- Nếu dùng emoji thì chỉ 0 hoặc 1 emoji
- Có thể dùng khẩu ngữ tự nhiên nếu hợp bài, ví dụ:
  + =)))
  + 😭
  + trời ơi
  + có mùi rồi nha
  + lộ quá rồi
  + căng thế
  + chịu luôn á
  + ai mà chịu nổi
  + nói vậy ai tin
  + nhìn là biết rồi
  + không ổn nha
  + tới công chuyện rồi
  + cười kiểu này là dở rồi

Cách định hướng bình luận:
- Nếu bài là meme/phim/tình huống hài: phản ứng vui, bắt đúng chi tiết gây cười
- Nếu bài có tình huống yêu đương/thả thính/couple: trêu nhẹ, tinh nghịch
- Nếu bài có drama nhẹ: hóng hớt vừa phải, không công kích
- Nếu bài cảm xúc: đồng cảm ngắn gọn, tự nhiên
- Nếu bài quảng bá phim/chương trình: bình luận như một người xem đang phản ứng vào nội dung thú vị của bài, không viết kiểu quảng cáo
- Nếu bài đăng không đủ ngữ cảnh, dữ liệu quét bị rác hoặc không hiểu được nội dung, trả về chính xác chuỗi:
SKIP_COMMENT

Dữ liệu bài viết:
- Page/account name: ${compactText(postData.accountName, 500) || '(không lấy được)'}
- Post text: ${compactText(postData.postText, 3500) || '(không lấy được)'}
- Hashtags: ${(postData.hashtags || []).join(', ') || '(không có)'}
- Image/video thumbnail text nếu có: ${compactText(postData.imageText, 2500) || '(không lấy được)'}

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
    .replace(/^Comment\s*:?\s*/i, '')
    .replace(/^Comment AI đề xuất\s*:?\s*/i, '')
    .trim();
}

function validateGeneratedComment(comment) {
  const sanitized = sanitizeOneLineComment(comment);
  if (!sanitized) {
    return { ok: false, reason: 'empty', comment: '' };
  }
  if (sanitized === SKIP_COMMENT) {
    return { ok: false, reason: 'skip', comment: sanitized };
  }
  if (BANNED_COMMENT_PATTERNS.some((pattern) => pattern.test(sanitized))) {
    return { ok: false, reason: 'generic', comment: sanitized };
  }
  if (sanitized.length > 220 || sanitized.split(/\s+/).length > 35) {
    return { ok: false, reason: 'too_long', comment: sanitized };
  }
  return { ok: true, reason: '', comment: sanitized };
}

async function generateComment(postData) {
  const prompt = buildCommentPrompt(postData);
  const content = await callChatCompletion({
    model: DEFAULT_COMMENT_MODEL,
    messages: [
      {
        role: 'system',
        content:
          'Bạn chỉ tạo đúng 1 bình luận Facebook tiếng Việt tự nhiên, bám sát ngữ cảnh bài viết. Nếu dữ liệu không đủ rõ để comment hợp lý, trả về đúng SKIP_COMMENT. Không giải thích.',
      },
      { role: 'user', content: prompt },
    ],
  });

  const validation = validateGeneratedComment(content);
  if (!validation.ok) {
    const error = new Error(`AI không trả về comment có thể đăng: ${validation.reason}`);
    error.code = validation.reason === 'skip' ? 'SKIP_COMMENT' : 'COMMENT_BLOCKED';
    error.reason = validation.reason;
    error.comment = validation.comment;
    throw error;
  }
  return validation.comment;
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
  BANNED_COMMENT_PATTERNS,
  SKIP_COMMENT,
  buildCommentPrompt,
  compactText,
  extractTextFromImages,
  generateComment,
  requireApiKey,
  sanitizeOneLineComment,
  validateGeneratedComment,
};
