#!/bin/bash
# Starts PolyCopy against a SIMULATED market: fake traders, fake money, no network trading.
# Use it to explore the web terminal safely. Data is kept separate from real trading.
cd "$(dirname "$0")" || exit 1
exec "./Start PolyCopy.command" --demo "$@"
