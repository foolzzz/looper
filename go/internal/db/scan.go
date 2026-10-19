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
// Now is the clock for a schedule's spent_today. Nil uses time.Now. The
// parity harness sets it to the same local noon Server.Now uses.
type Store struct {
	DB  *sql.DB
	Now func() time.Time
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
				val, err := coerceColumn(fd.DataTypeOID, vals[i])
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

// exec runs one statement. A nil store fails the same way query does.
// The context deadline is the statement deadline.
func (s *Store) exec(ctx context.Context, label, sql string, args ...any) error {
	if s == nil || s.DB == nil {
		return &safeError{msg: errUnreachable}
	}
	conn, err := s.DB.Conn(ctx)
	if err != nil {
		return fmt.Errorf("%s: %w", label, err)
	}
	defer conn.Close()

	err = conn.Raw(func(driverConn any) error {
		std, ok := driverConn.(*stdlib.Conn)
		if !ok {
			return fmt.Errorf("%s: unexpected driver connection", label)
		}
		_, err := std.Conn().Exec(ctx, sql, args...)
		return err
	})
	if err != nil {
		return fmt.Errorf("%s: %w", label, err)
	}
	return nil
}

// coerceColumn formats one cell. A date is YYYY-MM-DD, which is
// datetime.date.isoformat(). pgx decodes that OID as midnight UTC, and the
// generic time path would emit a timestamp. A numeric becomes millionths so
// task costs can sum it exactly. Other values go through coerce.
func coerceColumn(oid uint32, v any) (any, error) {
	if oid == pgtype.DateOID {
		switch t := v.(type) {
		case nil:
			return nil, nil
		case time.Time:
			return t.UTC().Format("2006-01-02"), nil
		default:
			return nil, fmt.Errorf("date column has %T", v)
		}
	}
	return coerce(v)
}

// coerce turns a pgx value into something encoding/json can write the way
// Python's json encoder writes the same cell: timestamps are Python's UTC
// isoformat, JSONB is already a decoded value, and numeric is millionths of
// a dollar (usdMicro) rather than a float.
func coerce(v any) (any, error) {
	switch t := v.(type) {
	case nil:
		return nil, nil
	case time.Time:
		return pyTime(t), nil
	case pgtype.Numeric:
		if !t.Valid {
			return nil, nil
		}
		m, err := numericMicros(t)
		if err != nil {
			return nil, err
		}
		return m, nil
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
