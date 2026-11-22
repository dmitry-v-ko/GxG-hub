-- KB entity feedback table
-- Stores like/dislike reactions + optional comments from admin UI.
-- Used by kb-retrospective skill to identify quality issues.

CREATE TABLE IF NOT EXISTS kb_entity_feedback (
  id          bigserial    PRIMARY KEY,
  entity_id   bigint       NOT NULL REFERENCES kb.entities(id) ON DELETE CASCADE,
  reaction    text         NOT NULL CHECK (reaction IN ('like', 'dislike')),
  comment     text,
  created_at  timestamptz  NOT NULL DEFAULT now()
);

CREATE INDEX idx_kb_entity_feedback_entity_id ON kb_entity_feedback(entity_id);
CREATE INDEX idx_kb_entity_feedback_reaction  ON kb_entity_feedback(reaction);

-- Open read/write for anon — admin UI is behind Basic Auth at kb.agents.gxg-labs.com
ALTER TABLE kb_entity_feedback ENABLE ROW LEVEL SECURITY;
CREATE POLICY "feedback_read_all"   ON kb_entity_feedback FOR SELECT USING (true);
CREATE POLICY "feedback_insert_all" ON kb_entity_feedback FOR INSERT WITH CHECK (true);
CREATE POLICY "feedback_delete_all" ON kb_entity_feedback FOR DELETE USING (true);
