# -*- coding: utf-8 -*-
import sys
sys.path.insert(0, r"D:\workplace2\dsh-workspace\_scratch")
sys.path.insert(0, r"D:\workplace2\dsh-workspace\_scratch\directions\width16")
from width16_hold import FastSession
s = FastSession()
if not s.open():
    print("open fail"); sys.exit(2)
if not s.find():
    print("no p104"); sys.exit(3)
cap = s.cap
def c(off): return s.prd(s.loc[0], s.loc[1], s.loc[2], cap + off)
print("LNKCAP =0x%08X" % (c(0x0C) or 0))
print("LNKCAP2=0x%08X" % (c(0x2C) or 0))
print("LNKSTA =0x%08X" % (c(0x12) or 0))
for off in (0x8872C, 0x8C040, 0x8C1C0, 0x8841C, 0x88610, 0x2241C):
    print("0x%06X = 0x%08X" % (off, s.rd(off) or 0))
s.close()
