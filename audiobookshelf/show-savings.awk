{ old += $1; new += $2 }
END {
  saved = old - new
  pct = (old > 0) ? (saved * 100.0 / old) : 0
  printf("\nTOTAL  Old: %.2fGB  New: %.2fGB  Saved: %.2fGB  (%.1f%%)\n",
         old/1073741824, new/1073741824, saved/1073741824, pct)
}
