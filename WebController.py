from urllib.request import urlopen
import csv, os, re

insttype_list = ["Marketplace","List","Company","News Agency","Equity",
                  "Derivative","Index","Exchange Traded Fund","Mutual Fund",
                  "Rights","Forex","Fixed Income","Money Market","Real Estate",
                  "Structured Product","Warrant","Uncategorized Type",
                  "Exchange Traded Commodity","Unit Trust Certificate",
                  "Primary Capital Certificate","Classification Sector",
                  "Commodity","Exchange Traded Certificate",
                  "Tick Table","Submarket","Implied Volatility Instruments"]

stocklist = []

def main():
    urlcheck(140000, 1000000)
    folder()
    os.chdir("Instrumenttype")
    i = 0
    while i < len(stocklist): 
        dircheck(stocklist[i])
        i += 1

def folder():
    while True:
        try:
            os.mkdir("Instrumenttype")
            os.chdir("Instrumenttype")
            i = 0
            while i < len(insttype_list):
                os.mkdir(insttype_list[i])
                i += 1
            os.chdir("../")
        except FileExistsError:
            break
    return

def dircheck(stock):
    insref,name,tradecurrency,instrumenttype= stock[:]
    os.chdir(insttype_list[instrumenttype])
    while True:
        try:
            os.chdir(tradecurrency)
        except FileNotFoundError:
            os.mkdir(tradecurrency)
            os.chdir(tradecurrency)
        break
    while True:
        try:
            os.mkdir(f"{insref}_{name}")
        except FileExistsError:
            break
    os.chdir(f'../../')
    return

def urlinfo(page):
    html_bytes = page.read()
    html = html_bytes.decode("utf-8")
    end= ":["
    info = html[17: html.find(end)].split(',')
    insref = info[0].split(':')
    name = info[1].split(':')
    tradecurrency = info[2].split(':')
    instrumenttype = info[5].split(':')
    stock = [int(insref[1]), name[1].replace('\\','').replace('/','').strip('\"'), 
             tradecurrency[1].strip("\""), int(instrumenttype[1])]
    stocklist.append(stock)

def urlcheck(start, end):
    i = start
    while i < end :
        url = f"https://mws-2.millistream.com/mws.fcgi?widget=intradaychart&token=0a4a41df-ed7b-4a36-b4c5-5a9706613825&target=buildwidget_0&fields=name,tradecurrency,time,date,tradeprice,tradequantity,marketopen,marketclose,closeprice1d&language=sv&compress=1&insref={i}&intradaylen=7&xhr=0&adjusted=1"
        while True:
            try:
                page = urlopen(url)
                urlinfo(page)
                break
            except:
                break
        if i%100 == 0:
            print(i)
            print(len(stocklist))
        i += 1
    return

main()
