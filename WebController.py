from urllib.request import urlopen
import csv, os, re, time, datetime,threading

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
    start = 2000000
    end = 3000000
    print(f"Checking Millistream if any stocks are found between {start} and {end}")
    starttime = time.time()
    multi(start, end)
    folder()
    i = 0
    while i < len(stocklist): 
        dircheck(stocklist[i])
        i += 1
    endtime = time.time()
    totaltime = datetime.timedelta(seconds=int(endtime-starttime))
    print(f"The time of execution of between {start} and {end} program is {totaltime} and number of stocks found {len(stocklist)}")

def multi(start, end):
    stack = (end - start) // 4
    thread0 = threading.Thread(target=urlcheck, args=(start, (start+stack), 0))
    thread1 = threading.Thread(target=urlcheck, args=((start+stack), (start+stack*2), 1))
    thread2 = threading.Thread(target=urlcheck, args=((start+stack*2), (start+stack*3), 2))
    thread3 = threading.Thread(target=urlcheck, args=((start+stack*3), end, 3))

    thread0.start()
    thread1.start()
    thread2.start()
    thread3.start()

    thread0.join()
    thread1.join()
    thread2.join()
    thread3.join()

def folder():
    while True:
        try:
            os.mkdir("Instrumenttype")
            os.chdir("Instrumenttype")
            i = 0
            while i < len(insttype_list):
                os.mkdir(insttype_list[i])
                i += 1
        except FileExistsError:
            os.chdir("Instrumenttype")
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
    clnname = re.sub(r'[^a-zA-Z0-9]', '', name[1])
    tradecurrency = info[2].split(':')
    instrumenttype = info[5].split(':')
    stock = [int(insref[1]), clnname, 
             tradecurrency[1].strip("\""), int(instrumenttype[1])]
    stocklist.append(stock)

def urlcheck(start, end, thread):
    starttime = time.time()
    i = starttime
    flag = False
    s = 0
    while start <= end :
        url = f"https://mws-2.millistream.com/mws.fcgi?widget=intradaychart&token=0a4a41df-ed7b-4a36-b4c5-5a9706613825&target=buildwidget_0&fields=name,tradecurrency,time,date,tradeprice,tradequantity,marketopen,marketclose,closeprice1d&language=sv&compress=1&insref={start}&intradaylen=7&xhr=0&adjusted=1"
        while True:
            try:
                page = urlopen(url)
                urlinfo(page)
                s += 1
                break
            except:
                break
        if start%1000 == 0 and flag:
            j = time.time()
            elapsedtime = datetime.timedelta(seconds=int(j-starttime))
            print(f"The time of execution of between {start-1000} and {start} on thread {thread} is: {round((j-i),3)} seconds, actual time {elapsedtime}")
            i = time.time()
            print(f"Number of stocks found {s} and total number of stocks in list {len(stocklist)}", "\n")
            s = 0
        start += 1
        flag = True
    return starttime

main()
