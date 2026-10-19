package db

import (
	"encoding/json"
	"math/big"
	"strconv"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgtype"
)

func TestNumericMicros(t *testing.T) {
	tests := []struct {
		name    string
		coeff   string
		exp     int32
		want    int64
		wantErr bool
		nan     bool
	}{
		{name: "tenth", coeff: "1", exp: -1, want: 100_000},
		{name: "fifth", coeff: "2", exp: -1, want: 200_000},
		{name: "one dollar", coeff: "1", exp: 0, want: 1_000_000},
		{name: "one and a half millionths rounds up", coeff: "15", exp: -7, want: 2},
		{name: "two and a half millionths stays even", coeff: "25", exp: -7, want: 2},
		{name: "half a millionth stays even", coeff: "5", exp: -7, want: 0},
		{name: "negative one and a half millionths", coeff: "-15", exp: -7, want: -2},
		{name: "zero", coeff: "0", exp: 0, want: 0},
		{name: "not a number", nan: true, wantErr: true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			n := pgtype.Numeric{Valid: true, InfinityModifier: pgtype.Finite, NaN: tt.nan}
			if !tt.nan {
				i, ok := new(big.Int).SetString(tt.coeff, 10)
				if !ok {
					t.Fatal(tt.coeff)
				}
				n.Int = i
				n.Exp = tt.exp
			}
			got, err := numericMicros(n)
			if tt.wantErr {
				if err == nil {
					t.Fatal("expected an error")
				}
				return
			}
			if err != nil || int64(got) != tt.want {
				t.Fatalf("numericMicros = %d, %v; want %d", got, err, tt.want)
			}
		})
	}
	if _, err := numericMicros(pgtype.Numeric{}); err == nil {
		t.Fatal("invalid numeric was accepted")
	}
}

func TestMicrosToFloatMatchesDecimal(t *testing.T) {
	got := microsToFloat(100_000 + 200_000)
	want, err := strconv.ParseFloat("0.300000", 64)
	if err != nil {
		t.Fatal(err)
	}
	if got != want {
		t.Fatalf("got %v, want %v", got, want)
	}
	// Variables, so this is a float64 add. A constant 0.1+0.2 is folded to 0.3.
	tenth, fifth := 0.1, 0.2
	if got == tenth+fifth {
		t.Fatal("millionth sum matched a float64 sum")
	}
	neg := microsToFloat(-100_000 + -200_000)
	wantNeg, err := strconv.ParseFloat("-0.300000", 64)
	if err != nil {
		t.Fatal(err)
	}
	if neg != wantNeg || neg == -tenth+-fifth {
		t.Fatalf("negative sum %v, want %v", neg, wantNeg)
	}
	body, err := json.Marshal(usdMicro(100_000))
	if err != nil {
		t.Fatal(err)
	}
	if string(body) != "0.1" {
		t.Fatalf("json %s", body)
	}
}

func TestCoerceDate(t *testing.T) {
	got, err := coerceColumn(pgtype.DateOID, time.Date(2020, 4, 2, 0, 0, 0, 0, time.UTC))
	if err != nil || got != "2020-04-02" {
		t.Fatalf("date %v %v", got, err)
	}
	got, err = coerceColumn(pgtype.TimestamptzOID, time.Date(2020, 4, 2, 0, 0, 0, 0, time.UTC))
	if err != nil || got != "2020-04-02T00:00:00+00:00" {
		t.Fatalf("timestamp %v %v", got, err)
	}
	if _, err := coerceColumn(pgtype.DateOID, "2020-04-02"); err == nil {
		t.Fatal("a non-time date was accepted")
	}
}
