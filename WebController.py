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

def dircheck(stock):
    instrument = stock[3]
    tradecurrency = stock[2]
    os.chdir("Instrumenttype")
    os.chdir(insttype_list[instrument])
    folder = os.listdir()
    print(folder)
    # for i in folder:
    #     if i == insttype_list[instrument]:
    #         print(instrument)
    #         print(tradecurrency)

def main():
    # urlcheck(1000, 10000)
    dircheck([1,1,1,3])



def urlinfo(page):
    html_bytes = page.read()
    html = html_bytes.decode("utf-8")
    end= ":["
    info = html[17: html.find(end)].split(',')
    insref = info[0].split(':')
    name = info[1].split(':')
    tradecurrency = info[2].split(':')
    instrument = info[5].split(':')
    stock = [int(insref[1]), name[1], tradecurrency[1], int(instrument[1])]
    stocklist.append(stock)
    print(stock)


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
        i += 1


main()
