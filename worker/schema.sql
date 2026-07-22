-- Q&A 질문 테이블
CREATE TABLE IF NOT EXISTS questions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  author TEXT DEFAULT '익명',
  content TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now', '+9 hours'))
);

-- Q&A 답변 테이블
CREATE TABLE IF NOT EXISTS answers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  question_id INTEGER NOT NULL,
  author TEXT DEFAULT '익명',
  content TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now', '+9 hours')),
  FOREIGN KEY (question_id) REFERENCES questions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_answers_qid ON answers(question_id);
CREATE INDEX IF NOT EXISTS idx_questions_created ON questions(created_at DESC);
