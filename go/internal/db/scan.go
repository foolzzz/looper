package db

import (
	"context"
	"database/sql"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5/pgtype"
	"github.com/jackc/pgx/v5/stdlib"
)

// Store reads the Looper tables. The HTTP layer turns these values into JSON.
type Store struct {
	DB *sql.DB
}

// NewStore wraps a pool opened with Open. A nil DB makes every read fail.
func NewStore(sqlDB *sql.DB) *Store {
	return &Store{DB: sqlDB}
}

func (s *Store) query(ctx context.Context, label, sql string, args ...any) ([]map[string]any, error) {
	if s == nil || s.DB == nil {
		return nil, &safeError{msg: errUnreachable}
	}
	conn, err := s.DB.Conn(ctx)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", label, err)
	}
	defer conn.Close()

	var out []map[string]any
	err = conn.Raw(func(driverConn any) error {
		std, ok := driverConn.(*stdlib.Conn)
		if !ok {
			return fmt.Errorf("%s: unexpected driver connection", label)
		}
		rows, err := std.Conn().Query(ctx, sql, args...)
		if err != nil {
			return err
		}
		defer rows.Close()
		fields := rows.FieldDescriptions()
		for rows.Next() {
			vals, err := rows.Values()
			if err != nil {
				return err
			}
			row := make(map[string]any, len(fields))
			for i, fd := range fields {
				val, err := coerce(vals[i])
				if err != nil {
					return fmt.Errorf("column %s: %w", fd.Name, err)
				}
				row[fd.Name] = val
			}
			out = append(out, row)
		}
		return rows.Err()
	})
	if err != nil {
		return nil, fmt.Errorf("%s: %w", label, err)
	}
	if out == nil {
		out = []map[string]any{}
	}
	return out, nil
}

// coerce turns a pgx value into something encoding/json can write the way
// Python's json encoder writes the same cell: timestamps are Python's UTC
// isoformat, JSONB is already a decoded value, numeric is a float.
func coerce(v any) (any, error) {
	switch t := v.(type) {
	case nil:
		return nil, nil
	case time.Time:
		return pyTime(t), nil
	case pgtype.Numeric:
		f, err := t.Float64Value()
		if err != nil {
			return nil, err
		}
		if !f.Valid {
			return nil, nil
		}
		return f.Float64, nil
	case map[string]any:
		out := make(map[string]any, len(t))
		for k, val := range t {
			next, err := coerce(val)
			if err != nil {
				return nil, err
			}
			out[k] = next
		}
		return out, nil
	case []any:
		out := make([]any, len(t))
		for i, val := range t {
			next, err := coerce(val)
			if err != nil {
				return nil, err
			}
			out[i] = next
		}
		return out, nil
	default:
		return v, nil
	}
}

// pyTime is datetime.isoformat() for an aware UTC value: no fractional
// seconds when the microseconds are zero, and "+00:00" rather than "Z".
func pyTime(t time.Time) string {
	t = t.UTC()
	base := t.Format("2006-01-02T15:04:05")
	if t.Nanosecond() != 0 {
		base = fmt.Sprintf("%s.%06d", base, t.Nanosecond()/1000)
	}
	return base + "+00:00"
}

// localISO is datetime.isoformat() for an aware local time: the offset keeps
// the process zone, the way overview.local_midnight().isoformat() does.
func localISO(t time.Time) string {
	t = t.In(t.Location())
	base := t.Format("2006-01-02T15:04:05")
	if t.Nanosecond() != 0 {
		base = fmt.Sprintf("%s.%06d", base, t.Nanosecond()/1000)
	}
	_, offset := t.Zone()
	sign := "+"
	if offset < 0 {
		sign = "-"
		offset = -offset
	}
	return fmt.Sprintf("%s%s%02d:%02d", base, sign, offset/3600, (offset%3600)/60)
}
