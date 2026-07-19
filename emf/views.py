from django.db.models import Q
from django.http import Http404
from django.http import HttpResponse, HttpResponseForbidden
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.conf import settings

from sqlalchemy.orm import joinedload, selectinload
from sqlalchemy import func

from decimal import Decimal

import datetime
from django.utils import timezone
import subprocess

import emf.models

from .tilldb import tillsession, booziness, on_tap
from quicktill.models import StockType, Unit, Department, \
    Payment, LogEntry, StockItem, StockOut
from quicktill.models import User as TillUser
from quicktill.models import Group as TillGroup


def current_time():
    # Override this when testing!
    # return datetime.datetime(2022, 6, 1, 17, 0, 0)
    return timezone.now()


def websocket_address(request):
    if settings.DEBUG:
        return "ws://localhost:8001/"
    scheme = "ws" if request.META['HTTP_HOST'][0].isdigit() else "wss"
    return f"{scheme}://{request.META['HTTP_HOST']}/websocket/"


class EventInfo:
    def __init__(self, now):
        # Work out how far through the event we are, based on the
        # supplied time.
        self.now = now
        sessions = emf.models.Session.objects.all()

        self.length = datetime.timedelta()
        self.total_consumption = 0.0
        self.time_passed = datetime.timedelta()
        self.expected_consumption = 0.0
        self.open = False
        self.next_open = None
        self.closes_at = None
        for s in sessions:
            start = s.opening_time
            end = s.closing_time
            weight = s.weight
            self.length += s.length
            self.total_consumption += weight
            if self.now >= end:
                # This segment has passed.
                self.time_passed += (end - start)
                self.expected_consumption += weight
            elif self.now >= start and self.now < end:
                # We are in this segment.
                self.open = True
                self.closes_at = end
                self.time_passed += (self.now - start)
                self.expected_consumption += weight * (
                    (self.now - start) / (end - start))
            elif self.now < start and not self.next_open:
                self.next_open = start
        if sessions:
            self.completed_fraction = self.time_passed / self.length
            self.completed_pct = self.completed_fraction * 100.0
            self.completed_pct_remainder = 100.0 - self.completed_pct
            self.expected_consumption_fraction = self.expected_consumption \
                / self.total_consumption
            self.expected_consumption_pct = self.expected_consumption_fraction \
                * 100.0
            self.expected_consumption_pct_remainder = 100.0 \
                - self.expected_consumption_pct
        else:
            self.completed_fraction = 0.0
            self.completed_pct = 0.0
            self.completed_pct_remainder = 100.0
            self.expected_consumption_fraction = 0.0
            self.expected_consumption_pct = 0.0
            self.expected_consumption_pct_remainder = 100.0


# We use this date format in templates - defined here so we don't have
# to keep repeating it.  It's available in templates as 'dtf'
dtf = "Y-m-d H:i"


@login_required
def database_dump(request):
    first_session = emf.models.Session.objects.all().first()
    if first_session and current_time() > first_session.opening_time:
        return HttpResponseForbidden("You can't dump the database after "
                                     "the start of the event.")

    with tillsession() as s:
        u = s.get_bind().url
    opts = ["pg_dump", "--no-owner"]
    if u.database:
        opts = opts + ["-d", u.database]
    if u.host:
        opts = opts + ["-h", u.host]
    if u.port:
        opts = opts + ["-p", str(u.port)]
    if u.username:
        opts = opts + ["-U", u.username]

    # We want to pipe pg_dump -O into gzip -9 and serve up the output
    # as application/octet-stream
    p1 = subprocess.Popen(opts, stdin=subprocess.PIPE if u.password else None,
                          stdout=subprocess.PIPE)
    p2 = subprocess.Popen(["gzip", "-9"], stdin=p1.stdout,
                          stdout=subprocess.PIPE)
    if u.password:
        p1.stdin.write(u.password.encode("utf-8") + b"\n")
        p1.stdin.close()
    p1.stdout.close()
    dump = p2.communicate()[0]

    r = HttpResponse(content_type="application/octet-stream")
    r['Content-Disposition'] = 'attachment; filename=emfcamp.sql.gz'
    r.write(dump)
    return r


def display(request, page=None):
    return render(request, 'emf/display.html')


def display_info(request):
    now = current_time()

    # Work out whether we are open
    sessions = emf.models.Session.objects.filter(
        closing_time__gt=now)
    currently_open = False
    for s in sessions:
        if s.opening_time < now:
            currently_open = True

    # Fetch pages
    pages = emf.models.DisplayPage.objects.filter(
        Q(display_after=None) | Q(display_after__lt=now),
        Q(display_until=None) | Q(display_until__gt=now),
        enabled=True,
        condition__in=('A', 'O' if currently_open else 'C')).all()

    urgent = [p for p in pages if p.priority == 'U']

    if urgent:
        # The only pages are urgent pages
        pages = [p.as_dict() for p in urgent]
    else:
        pages = [p.as_dict() for p in pages]

    if not pages:
        # Display is blank!
        return JsonResponse({
            'name': 'blank',
            'header': ' ',
            'content': ' ',
            'duration': 5000 if settings.DEBUG else 30000,
            'page': 'Idle',
            'count': 0,
            'current': 0,
        })

    current = request.GET.get("current", "start")

    pagenum = 0
    for pn, p in enumerate(pages):
        if p['name'] == current:
            pagenum = pn + 1
            break

    if pagenum >= len(pages):
        pagenum = 0

    page = pages[pagenum]
    page['page'] = f"Page {pagenum + 1} of {len(pages)}" \
        if len(pages) > 1 else ""

    page['count'] = len(pages)
    page['current'] = pagenum

    if callable(page['content']):
        page['content'] = page['content']()

    page['duration'] = 5000 if settings.DEBUG else page['duration'] * 1000

    return JsonResponse(page)


def display_page_info(request, page):
    try:
        page = emf.models.DisplayPage.objects.get(slug=page)
    except emf.models.DisplayPage.DoesNotExist:
        raise Http404

    page = page.as_dict()
    if callable(page['content']):
        page['content'] = page['content']()
    page['duration'] = 5000
    page['page'] = "Page n of m"

    return JsonResponse(page)


def frontpage(request):
    try:
        page = emf.models.Page.objects.get(path='')
        content = page.as_html()
    except emf.models.Page.DoesNotExist:
        content = ''

    with tillsession() as s:
        info = EventInfo(current_time())

        alcohol_used, total_alcohol, alcohol_used_pct = booziness(s)

        ales, kegs, ciders = on_tap(s)

        sessions = emf.models.Session.objects.filter(
            closing_time__gt=current_time())

        return render(
            request, "emf/whatson.html",
            context={
                "info": info,
                "alcohol_used": alcohol_used,
                "total_alcohol": total_alcohol,
                "alcohol_used_pct": alcohol_used_pct,
                "alcohol_used_pct_remainder": 100.0 - alcohol_used_pct,
                "sessions": sessions,
                "session_comments_exist": any(s.comment for s in sessions),
                "ales": ales,
                "kegs": kegs,
                "ciders": ciders,
                "content": content,
                "websocket_address": websocket_address(request),
            })


def pricelist(request):
    try:
        page = emf.models.Page.objects.get(path='pricelist')
        content = page.as_html()
    except emf.models.Page.DoesNotExist:
        content = ''

    # We filter out stock that has all gone here. In the future, we
    # may want to leave it in and filter it client-side based on live
    # updates
    with tillsession() as s:
        products = s\
            .query(StockType)\
            .join(Unit)\
            .join(Department)\
            .order_by(Department.id, StockType.manufacturer, StockType.name)\
            .filter(StockType.total_remaining > 0.0)\
            .all()

        return render(
            request, "emf/pricelist.html",
            context={
                "content": content,
                "products": products,
                "websocket_address": websocket_address(request),
            })


def product(request, stocktypeid):
    with tillsession() as s:
        stocktype = s.get(StockType, stocktypeid)

        if not stocktype:
            raise Http404

        return render(
            request, "emf/product.html",
            context={
                "product": stocktype,
                "key": f"stocktype/{stocktype.id}",
                "websocket_address": websocket_address(request),
            })


def tapboard(request):
    return render(request, "emf/tapboard.html", context={
        "websocket_address": websocket_address(request),
    })


def tapboard_sw(request):
    # The service worker needs to be scoped to '/' to allow it to access
    # /static/whatever
    response = render(request, "emf/tapboard-sw.js",
                      content_type="text/javascript")
    response.headers['Service-Worker-Allowed'] = '/'
    return response


def cellarboard(request, location):
    return render(request, "emf/cellarboard.html", context={
        "websocket_address": websocket_address(request),
        "location": location,
    })


def barboard(request):
    return render(request, "emf/barboard.html", context={
        "websocket_address": websocket_address(request),
    })


def jontyfacts(request):
    with tillsession() as s:
        pints_sold = s.query(func.sum(StockOut.qty)) \
            .select_from(StockOut)\
            .join(StockItem)\
            .join(StockType)\
            .join(Unit)\
            .filter(Unit.name == 'pint')\
            .filter(StockOut.removecode_id == 'sold')\
            .scalar()

        total_pints = s.query(func.sum(StockItem.size)) \
            .select_from(StockItem)\
            .join(StockType)\
            .join(Unit)\
            .filter(Unit.name == 'pint')\
            .scalar()

        volunteers = s.query(TillUser).count()

        card_payments = s.query(Payment)\
            .filter(Payment.paytype_id == 'SQUARE')\
            .count()

        cash_payments = s.query(Payment)\
            .filter(Payment.paytype_id == 'CASH')\
            .filter(Payment.amount > Decimal("0.00"))\
            .count()

        club_mate = s.query(func.sum(StockItem.used))\
            .select_from(StockItem)\
            .join(StockType)\
            .filter(StockType.dept_id == 75)\
            .scalar()

        return render(request, "emf/jontyfacts.html",
                      {"pints_sold": pints_sold,
                       "total_pints": total_pints,
                       "volunteers": volunteers - 1,  # remove 1 for "spacebar kiosk"
                       "card_payments": card_payments,
                       "cash_payments": cash_payments,
                       "club_mate": club_mate,
                       })


@login_required
def stocktypes(request):
    with tillsession() as s:
        stocktypes = s.query(StockType)\
                      .options(joinedload(StockType.meta),
                               joinedload(StockType.department),
                               selectinload(StockType.items))\
                      .filter(StockType.archived == False)\
                      .order_by(StockType.dept_id,
                                StockType.manufacturer,
                                StockType.name)\
                      .all()

        return render(request, "emf/stocktypes.html",
                      {"stocktypes": stocktypes,
                       })


@login_required
def tillprofile(request):
    with tillsession() as s:
        tilluser = s.query(TillUser)\
                    .options(joinedload(TillUser.permissions))\
                    .filter(TillUser.webuser == request.user.username)\
                    .one_or_none()

        available_users = s.query(TillUser)\
                           .filter(TillUser.webuser == None)\
                           .filter(TillUser.enabled == True)\
                           .order_by(TillUser.fullname)\
                           .all()

        if request.method == "POST" \
           and "submit_tillusersetup" in request.POST \
           and "tilluser" in request.POST:
            manager = s.get(TillGroup, "manager")
            if not manager:
                messages.error(
                    request,
                    "The till database does not have a \"manager\" group "
                    "defined — this must be set up before this function can "
                    "work.")
                return redirect("till-profile")
            if tilluser:
                tilluser.webuser = None
                s.commit()
            if request.POST["tilluser"]:
                newuser = s.get(TillUser, int(request.POST["tilluser"]))
            else:
                newuser = TillUser(fullname=request.user.get_full_name(),
                                   shortname=request.user.get_full_name(),
                                   enabled=True)
                s.add(newuser)
                s.commit()
            if not newuser:
                messages.error(
                    request,
                    f"Till user id {request.POST['tilluser']} not found")
                return redirect("user-profile-page")
            newuser.webuser = request.user.username
            if manager not in newuser.groups:
                newuser.groups.append(manager)
            le = LogEntry(
                source="Web",
                sourceaddr=request.META['REMOTE_ADDR'],
                loguser=newuser,
                user=newuser,
                description=f"Linked to web account '{request.user.username}'")
            le.update_refs(s)
            s.add(le)
            s.commit()
            return redirect("tillweb-pubroot")

        return render(request, "emf/tillprofile.html",
                      {'tilluser': tilluser,
                       'available_users': available_users,
                       })


# API views that do not access the till database
def api_sessions(request):
    sessions = emf.models.Session.objects.all()
    return JsonResponse({
        'sessions': [
            {
                "type": "session",
                "opening_time": timezone.localtime(s.opening_time),
                "closing_time": timezone.localtime(s.closing_time),
            } for s in sessions],
    })


def api_progress(request):
    with tillsession() as s:
        alcohol_used, total_alcohol, alcohol_used_pct = booziness(s)
        info = EventInfo(current_time())

        return JsonResponse({
            'licensed_time_pct': Decimal(info.completed_pct),
            'expected_consumption_pct': Decimal(info.expected_consumption_pct),
            'actual_consumption_pct': Decimal((alcohol_used / total_alcohol)
                                              * 100),
        })
