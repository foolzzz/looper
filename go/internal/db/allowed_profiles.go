package db

import "context"

// allowedProfilesSQL and listProfilesSQL are the PROFILE_JSON reads behind
// projects.allowed_profiles and profile_registry.list_profiles.
const allowedProfilesSQL = `SELECT ` + profileJSON + ` AS profile FROM project_allowed_profiles l JOIN ` + profileJoin +
	` ON a.id = l.profile_id WHERE l.project_id = $1 ORDER BY a.name`

const listProfilesSQL = `SELECT ` + profileJSON + ` AS profile FROM ` + profileJoin + ` ORDER BY a.builtin DESC, a.name`

// AllowedProfiles is projects.allowed_profiles_view. An unknown project is
// ErrProjectNotFound. An empty stored list is not a restriction: choosable is
// every registered profile that can run the Worker or the Reviewer. A
// non-empty list restricts both profiles and choosable to that list.
func (s *Store) AllowedProfiles(ctx context.Context, projectID string) (map[string]any, error) {
	project, err := s.one(ctx, "project", `SELECT id FROM projects WHERE id = $1`, projectID)
	if err != nil {
		return nil, err
	}
	if project == nil {
		return nil, ErrProjectNotFound
	}
	listed, err := s.profileRows(ctx, "allowed profiles", allowedProfilesSQL, projectID)
	if err != nil {
		return nil, err
	}
	restricted := len(listed) > 0
	choosableRows := listed
	if !restricted {
		all, err := s.profileRows(ctx, "profiles", listProfilesSQL)
		if err != nil {
			return nil, err
		}
		choosableRows = make([]map[string]any, 0, len(all))
		for _, row := range all {
			if choosableProfile(row) {
				choosableRows = append(choosableRows, row)
			}
		}
	}
	profiles := make([]any, 0, len(listed))
	for _, row := range listed {
		profiles = append(profiles, allowedProfileView(row))
	}
	choosable := make([]any, 0, len(choosableRows))
	for _, row := range choosableRows {
		choosable = append(choosable, allowedProfileView(row))
	}
	return map[string]any{
		"project_id": projectID,
		"restricted": restricted,
		"profiles":   profiles,
		"choosable":  choosable,
	}, nil
}

func (s *Store) profileRows(ctx context.Context, label, query string, args ...any) ([]map[string]any, error) {
	rows, err := s.query(ctx, label, query, args...)
	if err != nil {
		return nil, err
	}
	out := make([]map[string]any, 0, len(rows))
	for _, row := range rows {
		profile, ok := asMap(row["profile"])
		if !ok {
			continue
		}
		out = append(out, profile)
	}
	return out, nil
}

func choosableProfile(row map[string]any) bool {
	for _, role := range servableRoles(textList(row["runtime_capabilities"])) {
		if role == "worker" || role == "reviewer" {
			return true
		}
	}
	return false
}

// allowedProfileView is projects.allowed_profile_view. runtime is the runtime
// type. roles are the Worker and Reviewer subset. Prices may be null.
func allowedProfileView(row map[string]any) map[string]any {
	effort := row["effort"]
	if effort == "" {
		effort = nil
	}
	roles := []any{}
	for _, role := range servableRoles(textList(row["runtime_capabilities"])) {
		if role == "worker" || role == "reviewer" {
			roles = append(roles, role)
		}
	}
	prices := filledPrices(row, catalogModels(row["provider_models"]), asString(row["model"]))
	var input, output any
	if prices[0] != nil {
		input = *prices[0]
	}
	if prices[1] != nil {
		output = *prices[1]
	}
	return map[string]any{
		"profile_id": row["id"],
		"name":       row["name"],
		"runtime":    row["runtime_type"],
		"model":      row["model"],
		"effort":     effort,
		"roles":      roles,
		"usd_per_million_tokens": map[string]any{
			"input":  input,
			"output": output,
		},
	}
}
