package db

import "context"

// ListActivity is activity.list_activity. since is exclusive. limit is the
// value Python already passed to SQL, including 0 and a negative limit.
func (s *Store) ListActivity(ctx context.Context, taskID string, since, limit int64) ([]map[string]any, error) {
	// metadata::text is asyncpg's default jsonb value: the text, not a decoded
	// object. list_activity does not json.loads it, so the HTTP body keeps the
	// string. The rest of the statement is the Python query.
	return s.query(ctx, "activity", `
		SELECT seq, task_id, worker, kind, summary, metadata::text AS metadata,
		       input_tokens, output_tokens, created_at
		FROM activity
		WHERE task_id = $1 AND seq > $2
		ORDER BY seq ASC
		LIMIT $3`, taskID, since, limit)
}

// ActivityDetail is activity.get_activity_detail. The task id is not part of
// the query. A missing seq is a nil row and a nil error.
func (s *Store) ActivityDetail(ctx context.Context, seq int64) (map[string]any, error) {
	// Same columns as SELECT *, with metadata as text. See ListActivity.
	return s.one(ctx, "activity detail", `
		SELECT seq, task_id, worker, kind, summary, detail,
		       metadata::text AS metadata, input_tokens, output_tokens, created_at
		FROM activity
		WHERE seq = $1`, seq)
}

// ListConversation is activity.list_conversation.
func (s *Store) ListConversation(ctx context.Context, taskID string) ([]map[string]any, error) {
	return s.query(ctx, "conversation", `
		SELECT seq, worker, kind, summary, created_at
		FROM activity
		WHERE task_id = $1 AND kind IN ('text', 'phase', 'error', 'user_message')
		ORDER BY seq ASC`, taskID)
}
