# Day 1 (27 Oct): parts made fresh, need vs stock (v46k)

Every part that had to be made fresh for at least one day-1 trolley (27 Oct 07:00 to 28 Oct 07:00).

- **Day-1 need**: what all the day-1 trolleys that use this part need.
- **In stock**: the 27 Oct 06:30 stock of this part, in the same unit as the need. Treads are stocked in pieces and converted with the BOM metres per piece.
- **Expired**: past its shelf life at 07:00.
- **Usable**: in stock minus expired.
- **Used**: real stock used by day-1 trolleys.
- **Assumed**: the assumed first-8-hours cover (A-WIP-2), not real stock.
- **Made fresh**: what was made fresh for day 1.
- **Holds trolley**: the longest a trolley waits for this part.

## PCR

| Part | Type | Made on | Day-1 trolleys | Unit | Day-1 need | In stock | Expired | Usable | Used | Assumed | Made fresh | Holds trolley | Why not from stock |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| EG02 | Steel Belt Edge Strip | 1102 | 7 | m | 164 | 126 | 41 | 86 | 86 | 56 | **22** | 19.4 h | stock short of day-1 need |
| TRD 1925 XPC1 | TREAD | 1002 | 3 | m | 1,624 | 1,306 (637 pcs) | 0 | 1,306 | 1,306 | — | **318** | 15.5 h | stock short of day-1 need |
| SWRS44 | SIDEWALL | 1001 | 3 | m | 977 | 979 | 130 | 849 | 849 | — | **129** | 13.2 h | stock short of day-1 need |
| SWRS103 | SIDEWALL | 1001 | 2 | m | 1,241 | 746 | 131 | 614 | 614 | — | **626** | 13.1 h | stock short of day-1 need |
| TRD 2167 RAN HPE | TREAD | 1002 | 1 | m | 404 | 1 | 1 | 0 | 0 | — | **404** | 12.8 h | all stock expired before 07:00 |
| SWRS25 | SIDEWALL | 1001 | 5 | m | 1,955 | 1,334 | 3 | 1,330 | 1,330 | — | **625** | 12.4 h | stock short of day-1 need |
| SWRS131 | SIDEWALL | 1001 | 1 | m | 297 | 118 | 2 | 116 | 116 | — | **181** | 12.3 h | stock short of day-1 need |
| TRD 1995 ROYL KIA | TREAD | 1002 | 1 | m | 403 | 166 | 166 | 0 | 0 | — | **403** | 12.3 h | all stock expired before 07:00 |
| SWRS127 | SIDEWALL | 1001 | 1 | m | 259 | 206 | 0 | 206 | 206 | — | **53** | 12.2 h | stock short of day-1 need |
| SWRS105 | SIDEWALL | 1001 | 1 | m | 430 | 101 | 0 | 101 | 101 | — | **329** | 11.6 h | stock short of day-1 need |
| IL043 | IL | 1101 | 3 | m | 695 | 57 | 0 | 57 | 57 | — | **637** | 11.3 h | stock short of day-1 need |
| ECOPOLY350 | CALANDARED ROLL | 901 | 22 | m | 4,616 | 5,333 | 52 | 5,281 | 2,930 | — | **1,686** | 10.0 h | enough stock, but 2,351 given to later days (24 h buffer rule) |
| BA16009 | BEAD APEX | 201 | 4 | nos | 1,634 | 185 | 0 | 185 | 185 | — | **1,449** | 9.6 h | stock short of day-1 need |
| SWRS107 | SIDEWALL | 1001 | 3 | m | 1,093 | 962 | 37 | 925 | 925 | — | **168** | 9.3 h | stock short of day-1 need |
| SWRS42 | SIDEWALL | 1001 | 2 | m | 743 | 324 | 80 | 244 | 244 | — | **499** | 9.2 h | stock short of day-1 need |
| IL026 | IL | 1101 | 2 | m | 723 | 122 | 0 | 122 | 122 | — | **601** | 8.6 h | stock short of day-1 need |
| SWRS02 | SIDEWALL | 1001 | 1 | m | 386 | 379 | 0 | 379 | 379 | — | **7** | 8.2 h | stock short of day-1 need |
| BA15004 | BEAD APEX | 201 | 3 | nos | 1,584 | 914 | 6 | 908 | 908 | — | **676** | 8.2 h | stock short of day-1 need |
| IL066 | IL | 1101 | 3 | m | 908 | 264 | 0 | 264 | 264 | — | **644** | 7.9 h | stock short of day-1 need |
| IL059 | IL | 1101 | 1 | m | 249 | 97 | 0 | 97 | 97 | — | **153** | 7.5 h | stock short of day-1 need |
| IL058 | IL | 1101 | 1 | m | 266 | 338 | 0 | 338 | 64 | — | **203** | 7.2 h | enough stock, but 274 given to later days (24 h buffer rule) |
| SWRS108 | SIDEWALL | 1001 | 3 | m | 816 | 154 | 0 | 154 | 154 | — | **662** | 6.9 h | stock short of day-1 need |
| IL034 | IL | 1101 | 2 | m | 619 | 544 | 12 | 533 | 533 | — | **86** | 6.9 h | stock short of day-1 need |
| IL023 | IL | 1101 | 1 | m | 248 | 17 | 0 | 17 | 17 | — | **230** | 6.8 h | stock short of day-1 need |
| IL006 | IL | 1101 | 1 | m | 441 | 231 | 0 | 231 | 231 | — | **210** | 6.5 h | stock short of day-1 need |
| BA17017 | BEAD APEX | 202 | 1 | nos | 400 | 55 | 54 | 1 | 1 | — | **399** | 6.5 h | stock short of day-1 need |
| SWRS100 | SIDEWALL | 1001 | 2 | m | 667 | 401 | 0 | 401 | 401 | — | **266** | 6.5 h | stock short of day-1 need |
| HTPOLY350 | CALANDARED ROLL | 901 | 12 | m | 1,843 | 1,710 | 173 | 1,537 | 1,014 | — | **829** | 6.5 h | stock short; 523 also given to later days (buffer rule) |
| IL002 | IL | 1101 | 1 | m | 309 | 235 | 0 | 235 | 235 | — | **74** | 6.3 h | stock short of day-1 need |
| SWRS101 | SIDEWALL | 1001 | 6 | m | 3,335 | 2,430 | 104 | 2,326 | 2,326 | — | **1,009** | 6.3 h | stock short of day-1 need |
| BA15012 | BEAD APEX | 202 | 1 | nos | 430 | 2 | 2 | 0 | 0 | — | **430** | 5.8 h | all stock expired before 07:00 |
| SWRS05 | SIDEWALL | 1001 | 3 | m | 1,222 | 634 | 255 | 379 | 379 | — | **843** | 5.7 h | stock short of day-1 need |
| TRD 1564 NEO | TREAD | 1002 | 1 | m | 628 | 881 (539 pcs) | 607 | 275 | 275 | — | **353** | 5.2 h | stock short of day-1 need |
| BA14008 | BEAD APEX | 202 | 2 | nos | 1,148 | 298 | 0 | 298 | 298 | — | **850** | 5.0 h | stock short of day-1 need |
| IL010 | IL | 1101 | 1 | m | 358 | 267 | 0 | 267 | 267 | — | **91** | 4.5 h | stock short of day-1 need |
| BA13001 | BEAD APEX | 202 | 2 | nos | 2,042 | 787 | 0 | 787 | 787 | — | **1,255** | 4.4 h | stock short of day-1 need |
| IL007 | IL | 1101 | 2 | m | 1,021 | 494 | 0 | 494 | 494 | — | **527** | 4.0 h | stock short of day-1 need |
| BA17004 | BEAD APEX | 201 | 3 | nos | 1,778 | 922 | 7 | 915 | 915 | — | **863** | 4.0 h | stock short of day-1 need |
| BA14006 | BEAD APEX | 202 | 2 | nos | 1,482 | 544 | 33 | 511 | 511 | — | **971** | 3.9 h | stock short of day-1 need |
| BA12004 | BEAD APEX | 202 | 1 | nos | 676 | 403 | 298 | 105 | 105 | — | **571** | 3.2 h | stock short of day-1 need |
| TRD 2366 ROYL | TREAD | 1002 | 1 | m | 417 | 38 (18 pcs) | 0 | 38 | 38 | — | **379** | 3.1 h | stock short of day-1 need |
| BA13003 | BEAD APEX | 202 | 3 | nos | 1,816 | 277 | 0 | 277 | 277 | — | **1,539** | 2.3 h | stock short of day-1 need |
| TRD 1844 UXT | TREAD | 1002 | 1 | m | 720 | 623 (354 pcs) | 2 | 621 | 621 | — | **99** | 2.2 h | stock short of day-1 need |
| IL018 | IL | 1101 | 1 | m | 282 | 344 | 75 | 269 | 269 | — | **13** | 2.1 h | stock short of day-1 need |
| IL056 | IL | 1101 | 6 | m | 2,745 | 1,539 | 0 | 1,539 | 1,539 | — | **1,206** | 1.9 h | stock short of day-1 need |
| BA16002 | BEAD APEX | 201 | 1 | nos | 400 | 2 | 0 | 2 | 2 | — | **398** | 1.6 h | stock short of day-1 need |
| IL032 | IL | 1101 | 4 | m | 1,174 | 1,061 | 0 | 1,061 | 1,061 | — | **112** | 1.6 h | stock short of day-1 need |
| SWRS43 | SIDEWALL | 1001 | 4 | m | 1,465 | 1,055 | 105 | 950 | 950 | — | **515** | 1.6 h | stock short of day-1 need |
| BA12003 | BEAD APEX | 202 | 4 | nos | 2,566 | 1,605 | 0 | 1,605 | 1,605 | — | **961** | 1.2 h | stock short of day-1 need |
| TRD 2267 ROYL HYU | TREAD | 1002 | 3 | m | 1,798 | 1,733 (857 pcs) | 4 | 1,729 | 1,729 | — | **69** | 1.2 h | stock short of day-1 need |
| EHT1000 | CALANDARED ROLL | 901 | 4 | m | 1,079 | 1,112 | 25 | 1,087 | 807 | — | **272** | 0.9 h | enough stock, but 280 given to later days (24 h buffer rule) |
| BPG01 | Steel Belt Edge Strip | 1102 | 8 | m | 205 | 64 | 1 | 63 | 63 | — | **142** | 0.7 h | stock short of day-1 need |
| BA14009 | BEAD APEX | 201, 202 | 2 | nos | 1,030 | 274 | 0 | 274 | 274 | — | **756** | 0.7 h | stock short of day-1 need |
| HIRAD | CALANDARED ROLL | 901 | 6 | m | 1,453 | 1,191 | 63 | 1,128 | 1,128 | — | **326** | 0.0 h | stock short of day-1 need |
| BA16005 | BEAD APEX | 202 | 2 | nos | 1,168 | 413 | 116 | 297 | 297 | — | **871** | 0.0 h | stock short of day-1 need |
| EG01 | Steel Belt Edge Strip | 1102 | 29 | m | 1,051 | 706 | 118 | 589 | 589 | 331 | **132** | 0.0 h | stock short of day-1 need |
| BA14002 | BEAD APEX | 201 | 1 | nos | 400 | 294 | 294 | 0 | 0 | — | **400** | 0.0 h | all stock expired before 07:00 |
| BA13006 | BEAD APEX | 201 | 6 | nos | 5,490 | 2,895 | 0 | 2,895 | 2,895 | — | **2,595** | 0.0 h | stock short of day-1 need |
| BA18004 | BEAD APEX | 201 | 4 | nos | 1,614 | 569 | 1 | 568 | 568 | — | **1,046** | 0.0 h | stock short of day-1 need |
| BA14005 | BEAD APEX | 201 | 1 | nos | 768 | 338 | 95 | 243 | 243 | — | **525** | 0.0 h | stock short of day-1 need |
| TRD 1684 ROYL | TREAD | 1002 | 1 | m | 371 | 340 | 340 | 0 | 0 | — | **371** | 0.0 h | all stock expired before 07:00 |
| TRD 1583 NEO TATA | TREAD | 1002 | 3 | m | 1,539 | 1,244 (734 pcs) | 53 | 1,192 | 1,192 | — | **347** | 0.0 h | stock short of day-1 need |
| TRD 1503 NEO MSIL | TREAD | 1002 | 2 | m | 1,731 | 1,454 (858 pcs) | 0 | 1,454 | 1,454 | — | **276** | 0.0 h | stock short of day-1 need |
| TRD 1513 XPC1 MSIL | TREAD | 1002 | 6 | m | 4,721 | 3,161 (1,838 pcs) | 0 | 3,161 | 3,161 | — | **1,560** | 0.0 h | stock short of day-1 need |
| TRD 1452 XPC MM | TREAD | 1002 | 1 | m | 312 | 257 (165 pcs) | 0 | 257 | 257 | — | **55** | 0.0 h | stock short of day-1 need |
| TRD 1854 XPC TT | TREAD | 1002 | 1 | m | 381 | 156 (82 pcs) | 0 | 156 | 156 | — | **225** | 0.0 h | stock short of day-1 need |
| TRD 2476 SUP MM | TREAD | 1002 | 3 | m | 1,268 | 18 | 18 | 0 | 0 | — | **1,268** | 0.0 h | all stock expired before 07:00 |
| TRD 2776 RAN AT | TREAD | 1002 | 2 | m | 1,261 | 233 (108 pcs) | 2 | 231 | 231 | — | **1,030** | 0.0 h | stock short of day-1 need |

**PCR summary:** 68 parts. stock short of day-1 need: 58; all stock expired before 07:00: 6; enough stock: 3; stock short: 1.

## TBR

| Part | Type | Made on | Day-1 trolleys | Unit | Day-1 need | In stock | Expired | Usable | Used | Assumed | Made fresh | Holds trolley | Why not from stock |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| TR5096 | TREAD | 1801 | 3 | m | 891 | 210 (65 pcs) | 3 | 207 | 207 | — | **685** | 20.4 h | stock short of day-1 need |
| TR5003 | TREAD | 1801 | 2 | m | 724 | 98 (31 pcs) | 0 | 98 | 98 | — | **627** | 20.1 h | stock short of day-1 need |
| TR5020 | TREAD | 1801 | 1 | m | 274 | 237 (78 pcs) | 6 | 231 | 231 | — | **43** | 19.8 h | stock short of day-1 need |
| TR5079 | TREAD | 1801 | 1 | m | 252 | 227 (83 pcs) | 0 | 227 | 227 | — | **25** | 19.7 h | stock short of day-1 need |
| TR5025 | TREAD | 1801 | 2 | m | 541 | 52 (16 pcs) | 0 | 52 | 52 | — | **490** | 18.0 h | stock short of day-1 need |
| TPA-020 | PRE ASSEMBLY | 2201 | 7 | m | 1,038 | 184 | 0 | 184 | 184 | — | **854** | 16.8 h | stock short of day-1 need |
| TR5002 | TREAD | 1801 | 1 | m | 272 | 19 (6 pcs) | 13 | 6 | 6 | — | **265** | 14.8 h | stock short of day-1 need |
| TR5054 | TREAD | 1801 | 1 | m | 251 | 0 | 0 | 0 | 0 | — | **251** | 14.7 h | not in stock |
| TR5069 | TREAD | 1801 | 2 | m | 548 | 519 (180 pcs) | 14 | 505 | 505 | — | **43** | 14.4 h | stock short of day-1 need |
| TR5095 | TREAD | 1801 | 1 | m | 297 | 6 (2 pcs) | 0 | 6 | 6 | — | **291** | 14.2 h | stock short of day-1 need |
| TBAPBD005 | APEXED BEAD | 2405 | 7 | nos | 1,348 | 246 | 4 | 242 | 242 | — | **1,106** | 13.7 h | stock short of day-1 need |
| TR5078 | TREAD | 1801 | 5 | m | 750 | 366 (117 pcs) | 3 | 362 | 362 | — | **388** | 13.4 h | stock short of day-1 need |
| TBAPBD006 | APEXED BEAD | 2404 | 1 | nos | 184 | 47 | 47 | 0 | 0 | — | **184** | 13.0 h | all stock expired before 07:00 |
| TPA-004 | PRE ASSEMBLY | 2201 | 8 | m | 1,173 | 402 | 0 | 402 | 402 | — | **770** | 12.6 h | stock short of day-1 need |
| TR5070 | TREAD | 1801 | 2 | m | 753 | 475 (164 pcs) | 6 | 469 | 469 | — | **284** | 12.5 h | stock short of day-1 need |
| TR5026 | TREAD | 1801 | 1 | m | 444 | 335 (104 pcs) | 0 | 335 | 335 | — | **109** | 12.0 h | stock short of day-1 need |
| TBAPBD001 | APEXED BEAD | 2403 | 5 | nos | 820 | 585 | 303 | 282 | 282 | — | **538** | 11.9 h | stock short of day-1 need |
| TR5071 | TREAD | 1801 | 1 | m | 253 | 0 | 0 | 0 | 0 | — | **253** | 11.8 h | not in stock |
| TR5007 | TREAD | 1801 | 4 | m | 1,156 | 562 (179 pcs) | 9 | 553 | 553 | — | **603** | 11.8 h | stock short of day-1 need |
| TPA-002 | PRE ASSEMBLY | 2201 | 4 | m | 691 | 275 | 0 | 275 | 275 | — | **415** | 10.0 h | stock short of day-1 need |
| TBAPBD008 | APEXED BEAD | 2404 | 6 | nos | 640 | 749 | 155 | 594 | 594 | — | **46** | 8.6 h | stock short of day-1 need |
| TPA-016 | PRE ASSEMBLY | 2201 | 2 | m | 230 | 121 | 0 | 121 | 121 | — | **109** | 0.5 h | stock short of day-1 need |
| TPA-003 | PRE ASSEMBLY | 2201 | 1 | m | 138 | 65 | 0 | 65 | 65 | — | **73** | 0.1 h | stock short of day-1 need |
| TPA-018 | PRE ASSEMBLY | 2201 | 1 | m | 155 | 47 | 0 | 47 | 47 | — | **108** | 0.1 h | stock short of day-1 need |
| TEG-01 | SLITTED MATERIAL | 1102 | 29 | m | 1,901 | 914 | 158 | 756 | 756 | 653 | **492** | 0.0 h | stock short of day-1 need |
| TPA-011 | PRE ASSEMBLY | 2201 | 1 | m | 135 | 0 | 0 | 0 | 0 | — | **135** | 0.0 h | not in stock |
| TBAPBD003 | APEXED BEAD | 2404 | 8 | nos | 1,528 | 1,359 | 84 | 1,276 | 1,276 | — | **252** | 0.0 h | stock short of day-1 need |
| EG02 | Steel Belt Edge Strip | 1102 | 29 | m | 1,342 | 807 | 133 | 674 | 674 | 469 | **200** | 0.0 h | stock short of day-1 need |
| BEF001 | BELT EDGE FILLER | 1802 | 29 | m | 7,677 | 6,880 | 469 | 6,411 | 6,411 | — | **1,266** | 0.0 h | stock short of day-1 need |
| TEG-02 | Steel Belt Edge Strip | 1102 | 29 | m | 587 | 214 | 37 | 176 | 176 | 220 | **190** | 0.0 h | stock short of day-1 need |
| TPA-024 | PRE ASSEMBLY | 2201 | 5 | m | 404 | 310 | 0 | 310 | 310 | — | **95** | 0.0 h | stock short of day-1 need |

**TBR summary:** 31 parts. stock short of day-1 need: 27; not in stock: 3; all stock expired before 07:00: 1.

