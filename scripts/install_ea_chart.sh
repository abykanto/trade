#!/usr/bin/env bash
# Attach TradeIdeaExecutor to the default XAUUSD chart profile (best-effort auto-start).
set -euo pipefail

WINEPREFIX="${WINEPREFIX:-$HOME/.wine}"
CHART="$WINEPREFIX/drive_c/Program Files/MetaTrader 5/MQL5/Profiles/Charts/Default/chart01.chr"
EA_PORT="${EA_SERVER_PORT:-19520}"

mkdir -p "$(dirname "$CHART")"
cat > "$CHART" <<EOF
<chart>
id=0
symbol=XAUUSD
description=Gold vs US Dollar
period_type=1
period_size=1
digits=2
tick_size=0.010000
position_time=0
scale_fix=0
scale_fixed_min=0.000000
scale_fixed_max=0.000000
scale_fix11=0
scale_bar=0
scale_bar_val=1.000000
scale=16
mode=1
fore=0
grid=1
volume=0
scroll=1
shift=0
shift_size=20.000000
fixed_pos=0.000000
ticker=1
ohlc=0
one_click=0
one_click_btn=1
bidline=1
askline=0
lastline=0
days=0
descriptions=0
tradelines=1
tradehistory=1
window_left=0
window_top=0
window_right=1200
window_bottom=800
window_type=1
floating=0
floating_left=0
floating_top=0
floating_right=0
floating_bottom=0
floating_type=1
floating_toolbar=1
floating_tbstate=
background_color=0
foreground_color=16777215
barup_color=65280
bardown_color=65280
bullcandle_color=0
bearcandle_color=16777215
chartline_color=65280
volumes_color=3329330
grid_color=10061943
bidline_color=10061943
askline_color=255
lastline_color=49152
stops_color=255
windows_total=1

<window>
height=100.000000
objects=0

<indicator>
name=Main
path=
apply=1
show_data=1
scale_inherit=0
scale_line=0
scale_line_percent=50
scale_line_value=0.000000
scale_fix_min=0
scale_fix_min_val=0.000000
scale_fix_max=0
scale_fix_max_val=0.000000
expertmode=0
fixed_height=-1
</indicator>
</window>

<expert>
name=TradeIdeaExecutor
path=Experts\\TradeIdeaExecutor.ex5
expertmode=1
<inputs>
InpPythonHost=127.0.0.1
InpPythonPort=$EA_PORT
InpMagicNumber=234000
InpDeviation=20
InpTimerMs=100
InpEnableLogging=true
</inputs>
</expert>
</chart>
EOF
echo "Installed EA chart profile: $CHART"
