const SKIP_COMMENT = 'SKIP_COMMENT';

const BANNED_COMMENT_PATTERNS = [
  /mình nghĩ nên/i,
  /từng tình huống/i,
  /mỗi người có thể/i,
  /góc nhìn khác nhau/i,
  /đáng suy ngẫm/i,
  /vấn đề thú vị/i,
  /rất đồng tình/i,
  // Formal/chatbot patterns
  /^hãy\s/i,
  /^hãy chúc/i,
  /^chúc bạn/i,
  /^chúc anh/i,
  /^chúc chị/i,
  /^tuyệt vời quá/i,
  /^hay quá/i,
  /^thật tuyệt/i,
];

// Từ khóa tôn giáo → bắt buộc SKIP hoặc dùng reply an toàn
const RELIGION_KEYWORDS = [
  /\bchúa\b/i,
  /\bamen\b/i,
  /\babba\b/i,
  /\bchúa giêsu\b/i,
  /\bthiên chúa\b/i,
  /\bphật\b/i,
  /\bniệm phật\b/i,
  /\bchúa ơi\b/i,
  /\bcha trời\b/i,
  /\bcha abba\b/i,
  /\blạy chúa\b/i,
  /\bơn chúa\b/i,
  /\bcảm ơn chúa\b/i,
];

// Câu bị lửng - kết thúc bằng từ hỏi/mơ hồ mà không có dấu ?
const DANGLING_ENDINGS = [
  /\s(ai|gì|nào|sao|đâu|nào|chi|thôi|là|mà|nhưng|vì|hay|hoặc)$/i,
];

function compactText(value, maxLength = 3000) {
  return String(value || '')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, maxLength);
}

function buildPostContextLines(postData) {
  return `- Page/account name: ${compactText(postData.accountName, 500) || '(không lấy được)'}
- Post text: ${compactText(postData.postText, 3500) || '(không lấy được)'}
- Hashtags: ${(postData.hashtags || []).join(', ') || '(không có)'}
- Image/video thumbnail text nếu có: ${compactText(postData.imageText, 2500) || '(chưa OCR / không lấy được chữ trong ảnh)'}`;
}

/**
 * Kiểm tra nội dung bài có liên quan đến tôn giáo không
 */
function isReligiousPost(postData) {
  const combined = [
    postData.accountName || '',
    postData.postText || '',
    postData.imageText || '',
  ].join(' ');
  return RELIGION_KEYWORDS.some((kw) => kw.test(combined));
}

function buildCommentPrompt(postData) {
  return `Bạn là một người Việt Nam 20 tuổi thường xuyên lướt Facebook và bình luận rất tự nhiên, đúng kiểu GenZ.

Nhiệm vụ:
Đọc kỹ toàn bộ ngữ cảnh của bài đăng Facebook, bao gồm:
- Tên page hoặc tài khoản đăng bài
- Nội dung caption/post text
- Hashtag
- Chữ trong ảnh hoặc thumbnail video nếu có
- Ảnh/thumbnail được đính kèm trong tin nhắn này nếu có

Sau đó viết ra đúng 1 bình luận phù hợp nhất với bài đăng.

Phong cách bình luận mong muốn:
- Kiểu Gen Z Việt Nam, tự nhiên, hơi đời, có cá tính
- Giống người thật lướt thấy bài rồi comment ngay, không cần suy nghĩ nhiều
- Bám rất sát nội dung cụ thể của bài, phản ứng đúng tình huống
- Có thể hùa theo, thả miếng, trêu nhẹ, cà khịa nhẹ, bắt đúng tình huống gây cười hoặc chi tiết nổi bật
- Ưu tiên những câu khiến người đọc thấy "comment này đúng bài ghê"
- Không viết như chatbot, không văn mẫu, không nghị luận dài dòng

Yêu cầu bắt buộc:
- Chỉ trả về đúng 1 comment duy nhất
- Không giải thích, không thêm dấu ngoặc kép, không tiền tố như "Comment:"
- Viết thành một câu hoàn chỉnh từ 7 đến 25 từ, có đủ chủ vị
- Không để câu bị lửng giữa chừng (ví dụ không được kết thúc bằng "ai", "gì", "nào" mà không có ngữ cảnh hoàn chỉnh)
- Không bắt đầu bằng "Hãy...", "Chúc...", "Thật tuyệt..." vì nghe rất giả
- Không lặp lại nguyên văn caption
- Không bịa thêm số liệu, con số, tên người, sự kiện không có trong bài
- Nếu bài có ít thông tin: bình luận đơn giản bám sát những gì có trong bài, không bịa thêm
- Không dùng kiểu giọng AI như:
  + "mình nghĩ nên nhìn theo từng tình huống thực tế"
  + "mỗi người có thể có một góc nhìn khác nhau"
  + "nội dung này rất đáng suy ngẫm"
  + "đây là một vấn đề thú vị"
  + "rất đồng tình với quan điểm này"
- Không dùng các lời khen rỗng như: "hay quá", "đỉnh thật", "tuyệt vời", "xịn nha" nếu không thực sự hợp ngữ cảnh
- Không lạm dụng emoji, nếu dùng thì chỉ 0 hoặc 1 emoji

Ví dụ comment đúng phong cách:
- "vẽ giống mà còn đẹp nữa, người ta thật sự có tài 🔥"
- "đến đây mà không cần giới thiệu, ai nhìn cũng nhận ra liền"
- "kiểu này là còn drama tiếp đây, chờ phần sau thôi"
- "nói vậy ai tin được, nhìn biểu cảm là hiểu hết rồi"
- "gen nhà mình là thế, không ai chịu nhường ai cả 😂"
- "ngồi đây hóng tiếp, chắc chắn có phần 2 rồi"

Cách định hướng bình luận:
- Nếu bài là meme/phim/tình huống hài: phản ứng vui, bắt đúng chi tiết gây cười
- Nếu bài có tình huống yêu đương/thả thính/couple: trêu nhẹ, tinh nghịch
- Nếu bài có drama nhẹ: hóng hớt vừa phải, không công kích
- Nếu bài cảm xúc: đồng cảm ngắn gọn, tự nhiên
- Nếu bài quảng bá phim/chương trình: bình luận như một người xem đang phản ứng vào nội dung thú vị của bài, không viết kiểu quảng cáo
- NẾU BÀI ĐĂNG THUỘC CÁC TRƯỜNG HỢP SAU ĐÂY, BẮT BUỘC TRẢ VỀ CHÍNH XÁC CHUỖI \`SKIP_COMMENT\`:
  + Nội dung tôn giáo: cầu nguyện, Chúa, Amen, Phật, kinh thánh, tín ngưỡng.
  + Nội dung tiêu cực: tai nạn, tử vong, đám tang, bệnh tật, than vãn buồn bã.
  + Các vấn đề nhạy cảm: chính trị, pháp luật, phân biệt vùng miền, chiến tranh, tranh cãi gay gắt.
  + Nội dung độc hại: lừa đảo, đa cấp, cờ bạc, 18+.
  + Dữ liệu quét bị lỗi, không có ý nghĩa, hoặc bạn cảm thấy bài viết không phù hợp để bình luận vui vẻ.

Dữ liệu bài viết:
${buildPostContextLines(postData)}

Hãy trả về đúng 1 bình luận phù hợp nhất.`;
}

function buildReplyPrompt(postData, targetComment) {
  return `Bạn là một người Việt Nam 20 tuổi thường xuyên lướt Facebook và trả lời comment rất tự nhiên, đúng kiểu GenZ.

Luồng bắt buộc:
Bước 1: Đọc kỹ nội dung bài viết và hình ảnh/thumbnail nếu có.
Bước 2: Đọc kỹ comment đang cần trả lời.
Bước 3: Viết đúng 1 câu phản hồi vừa liên quan tới bài viết, vừa ăn khớp trực tiếp với comment đó.

Nhiệm vụ:
Dựa trên cả 2 phần ngữ cảnh dưới đây, hãy viết 1 reply vào comment. Reply phải nghe như người thật đang phản hồi comment trong thread, không phải comment mới độc lập vào bài.

Phong cách mong muốn:
- Tiếng Việt tự nhiên, Gen Z vừa phải, hơi đời, có cá tính
- Bám sát chi tiết cụ thể của bài viết và ý của comment cần trả lời
- Có thể đồng tình, trêu nhẹ, nối ý, bắt miếng hoặc bổ sung ngắn gọn
- Không công kích cá nhân, không chửi tục nặng, không gây war
- Không viết như chatbot, không văn mẫu, không nghị luận dài

Yêu cầu bắt buộc:
- Chỉ trả về đúng 1 reply duy nhất
- Không giải thích, không thêm dấu ngoặc kép, không tiền tố như "Reply:" hoặc "Comment:"
- Viết thành một câu hoàn chỉnh từ 7 đến 25 từ, có đủ chủ vị
- Không để câu bị lửng giữa chừng
- Không bắt đầu bằng "Hãy...", "Chúc..." vì nghe rất giả tạo
- Không bịa thêm số liệu, con số, tên người, sự kiện không có trong bài hoặc comment
- Không lặp lại nguyên văn caption hoặc comment gốc
- Không trả lời chung chung nếu không hiểu bài/comment
- Nếu dữ liệu bài viết thiếu hoặc mơ hồ nhưng vẫn có comment cần trả lời, vẫn phải viết 1 reply an toàn bám theo ý comment đã quét
- Chỉ trả về SKIP_COMMENT khi comment cần trả lời hoàn toàn trống hoặc không đọc được

Dữ liệu bài viết:
${buildPostContextLines(postData)}

Comment cần trả lời:
- ${compactText(targetComment, 1200) || '(không lấy được)'}

Hãy trả về đúng 1 reply phù hợp với comment cần trả lời; nếu ngữ cảnh bài viết thiếu thì ưu tiên bám theo comment đã quét, không bỏ qua.`;
}

function sanitizeOneLineComment(text) {
  return compactText(text, 280)
    .replace(/^['\"""'']+|['\"""'']+$/g, '')
    .replace(/^Comment\s*:?\s*/i, '')
    .replace(/^Comment AI đề xuất\s*:?\s*/i, '')
    .replace(/^Reply\s*:?\s*/i, '')
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
  const wordCount = sanitized.split(/\s+/).length;
  if (wordCount < 7) {
    return { ok: false, reason: 'too_short', comment: sanitized };
  }
  if (sanitized.length > 220 || wordCount > 35) {
    return { ok: false, reason: 'too_long', comment: sanitized };
  }
  // Kiểm tra câu bị lửng (kết thúc bằng từ hỏi/mơ hồ mà không có dấu ?)
  if (DANGLING_ENDINGS.some((re) => re.test(sanitized))) {
    return { ok: false, reason: 'dangling_sentence', comment: sanitized };
  }
  return { ok: true, reason: '', comment: sanitized };
}

module.exports = {
  BANNED_COMMENT_PATTERNS,
  RELIGION_KEYWORDS,
  DANGLING_ENDINGS,
  SKIP_COMMENT,
  buildCommentPrompt,
  buildPostContextLines,
  buildReplyPrompt,
  compactText,
  isReligiousPost,
  sanitizeOneLineComment,
  validateGeneratedComment,
};
