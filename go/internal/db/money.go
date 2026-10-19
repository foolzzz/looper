package db

import (
	"encoding/json"
	"errors"
	"fmt"
	"math/big"
	"strconv"

	"github.com/jackc/pgx/v5/pgtype"
)

// usdMicro is a dollar amount in millionths, the unit Python sums after
// round(sum(cost_usd::numeric), 6). Task costs add these integers and convert
// once, the way float(Decimal) does. A float64 sum of 0.1 and 0.2 is a
// different value.
//
// MarshalJSON exists so a value that reaches encoding/json is that float, not
// the integer millionths. The method has a value receiver because map elements
// are not addressable. assembleCosts still writes float64 before it returns.
type usdMicro int64

func (m usdMicro) MarshalJSON() ([]byte, error) {
	return json.Marshal(microsToFloat(int64(m)))
}

var errNumeric = errors.New("numeric is not a finite dollar amount")

// numericMicros converts a PostgreSQL numeric to millionths.
// A scale finer than 1e-6 rounds half to even, which is what round(numeric, 6)
// already did for task costs. NaN, infinity, and a magnitude that does not fit
// in int64 are errors. The caller does not add them and does not panic.
func numericMicros(n pgtype.Numeric) (usdMicro, error) {
	if !n.Valid || n.NaN || n.InfinityModifier != pgtype.Finite {
		return 0, errNumeric
	}
	mag := new(big.Int)
	neg := false
	if n.Int != nil {
		neg = n.Int.Sign() < 0
		mag.Abs(n.Int)
	}
	if mag.Sign() == 0 {
		return 0, nil
	}
	shift := int64(n.Exp) + 6
	if shift > 1000 || shift < -1000 {
		return 0, errNumeric
	}
	if shift >= 0 {
		scale := new(big.Int).Exp(big.NewInt(10), big.NewInt(shift), nil)
		mag.Mul(mag, scale)
	} else {
		div := new(big.Int).Exp(big.NewInt(10), big.NewInt(-shift), nil)
		quot := new(big.Int)
		rem := new(big.Int)
		quot.QuoRem(mag, div, rem)
		twice := new(big.Int).Lsh(rem, 1)
		cmp := twice.Cmp(div)
		if cmp > 0 || (cmp == 0 && quot.Bit(0) == 1) {
			quot.Add(quot, big.NewInt(1))
		}
		mag = quot
	}
	if !mag.IsInt64() {
		return 0, errNumeric
	}
	v := mag.Int64()
	if neg {
		v = -v
	}
	return usdMicro(v), nil
}

// moneyMicros reads a cost cell. Nil, and anything that is not a dollar
// amount, is not added. float64 is not accepted: summing those is the drift
// this type exists to avoid.
func moneyMicros(v any) (int64, bool) {
	switch t := v.(type) {
	case usdMicro:
		return int64(t), true
	case pgtype.Numeric:
		m, err := numericMicros(t)
		if err != nil {
			return 0, false
		}
		return int64(m), true
	default:
		return 0, false
	}
}

// microsToFloat is float(Decimal) for a value already rounded to six places.
// The decimal text is parsed once. It is not added to another float, and it
// is not passed through roundN.
func microsToFloat(micro int64) float64 {
	sign := ""
	u := uint64(micro)
	if micro < 0 {
		sign = "-"
		u = uint64(-micro)
	}
	s := fmt.Sprintf("%s%d.%06d", sign, u/1_000_000, u%1_000_000)
	f, err := strconv.ParseFloat(s, 64)
	if err != nil {
		return 0
	}
	return f
}
