package db

import "context"

// ListRoleConfigs is role_configs.list_configs. The handler turns a failure
// into an empty list.
func (s *Store) ListRoleConfigs(ctx context.Context) ([]map[string]any, error) {
	return s.query(ctx, "role configs", `SELECT * FROM role_configs ORDER BY role`)
}

// GetRoleConfig is role_configs.get_config. A missing role is nil, nil.
func (s *Store) GetRoleConfig(ctx context.Context, role string) (map[string]any, error) {
	return s.one(ctx, "role config", `SELECT * FROM role_configs WHERE role = $1`, role)
}
