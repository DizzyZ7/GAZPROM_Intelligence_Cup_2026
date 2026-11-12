"""Minimal, dependency-free XLSB records reader for the uploaded BIFF12 numeric source."""
from zipfile import ZipFile
import struct, sys
from pathlib import Path
# Path supplied to the calling analysis script; no implicit environment dependency.

def recs(data):
    pos=0; end=len(data)
    while pos<end:
        rid=0
        for k in range(4):
            if pos >= end:
                raise ValueError('Некорректная запись BIFF12: отсутствует байт идентификатора')
            c=data[pos]; pos+=1; rid|=c<<(8*k)
            if not c&128:break
        n=0
        for k in range(4):
            if pos >= end:
                raise ValueError('Некорректная запись BIFF12: отсутствует байт длины')
            c=data[pos]; pos+=1; n|=(c&127)<<(7*k)
            if not c&128:break
        if pos + n > end:
            raise ValueError('Некорректная запись BIFF12: длина превышает границы файла')
        yield rid,memoryview(data)[pos:pos+n]
        pos+=n

def uint(buf,at=0):return struct.unpack_from('<I',buf,at)[0]
def wide(buf, at):
    n=uint(buf,at); at+=4
    s=buf[at:at+n*2].tobytes().decode('utf-16le',errors='replace'); return s,at+n*2

def number_rk(buf):
    intval=struct.unpack_from('<i',buf,8)[0]
    if intval&2: v=float(intval>>2)
    else: v=struct.unpack('<d',b'\x00\x00\x00\x00'+struct.pack('<I',intval&0xFFFFFFFC))[0]
    if intval&1:v/=100
    return v

def get_book(z):
    names=[]; shared=[]
    for r,d in recs(z.read('xl/workbook.bin')):
        if r==0x019c:
            _,i=wide(d,8); s,i=wide(d,i); names.append(s)
    for r,d in recs(z.read('xl/sharedStrings.bin')):
        if r==0x13:
            s,_=wide(d,1);shared.append(s)
    return names,shared

def sheet_rows(z,num,shared):
    current=None;row=[]
    for r,d in recs(z.read(f'xl/worksheets/sheet{num}.bin')):
        if r==0:
            if current is not None: yield current,row
            current=uint(d);row=[]
        elif r in (2,3,4,5,7,8,9,10,11) and current is not None:
            col=uint(d);style=uint(d,4)
            if r==2: val=number_rk(d)
            elif r==5 or r==9: val=struct.unpack_from('<d',d,8)[0]
            elif r==7: val=shared[uint(d,8)]
            elif r==8: val=wide(d,8)[0]
            elif r in (4,10): val=bool(d[8])
            else:val=None
            row.append((col,val))
        elif r==0x0192:
            if current is not None:yield current,row
            break
