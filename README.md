# eduVULCAN — integracja Home Assistant

Nieoficjalna integracja Home Assistant dla dziennika **eduVULCAN** (nowe API hebeCE firmy VULCAN).
Pobiera dla każdego dziecka: plan lekcji (z zastępstwami i odwołaniami), prace domowe,
sprawdziany i szczęśliwy numerek.

Oparta na zwendorowanej kopii biblioteki [bbrjpl1310b/iris](https://github.com/bbrjpl1310b/iris)
(AGPL-3.0). Zero zewnętrznych zależności — wszystko, czego potrzebuje (pydantic, cryptography,
aiohttp), jest już w Home Assistant Core.

> **Uwaga:** to nieoficjalne API. Korzystasz na własną odpowiedzialność — regulamin eduVULCAN
> nie przewiduje klientów zewnętrznych.

## Instalacja (HACS)

1. HACS → Integrations → menu (⋮) → **Custom repositories**
2. Dodaj `https://github.com/PiotrOrman/eduVulcan` jako typ **Integration**
3. Zainstaluj **eduVULCAN**, zrestartuj Home Assistant

## Konfiguracja

1. W przeglądarce zaloguj się na **https://eduvulcan.pl/api/ap**
2. Po zalogowaniu otwórz źródło strony (Ctrl+U / prawy klik → Zbadaj) i znajdź ukryte pole
   `<input type="hidden" value="...">` — jego wartość to JSON z listą `Tokens`
   (jeden token JWT na dziecko)
3. W HA: **Ustawienia → Urządzenia i usługi → Dodaj integrację → eduVULCAN**
4. Wklej cały JSON (albo same tokeny JWT) i zatwierdź

Tokeny są **jednorazowe** i służą tylko do rejestracji "wirtualnego telefonu" (klucz RSA).
Po rejestracji integracja odświeża dane sama — tokeny nie są nigdzie zapisywane.
Do planu, zadań, sprawdzianów i numerka hasło do eduVULCAN nie jest potrzebne.
Wiadomości wymagają go od listopada 2026 — patrz niżej.

## Encje

Dla każdego dziecka powstaje urządzenie `Szkoła <imię>` z sensorami:

| Encja | Stan | Atrybuty |
|---|---|---|
| `sensor.szkola_<imie>_nastepna_lekcja` | np. `Matematyka (08:00)` | `dzis`, `jutro` (pełny plan z salami, nauczycielami, zastępstwami), `nastepny_dzien_szkolny` + `nastepny_dzien_szkolny_plan` (najbliższy dzień z lekcjami — w weekend pokazuje poniedziałek), `nastepna_lekcja_start` |
| `sensor.szkola_<imie>_zadania_domowe` | liczba otwartych zadań | `zadania` (przedmiot, treść, termin, dni do terminu) |
| `sensor.szkola_<imie>_sprawdziany` | liczba nadchodzących | `sprawdziany` (przedmiot, typ, treść, data) |
| `sensor.szkola_<imie>_szczesliwy_numerek` | numerek | `numer_w_dzienniku`, `trafiony` |

Dane odświeżane co 30 minut.

## Wiadomości ze szkoły (moduł WWW)

> ⚠️ **Hasło w Home Assistant.** Ta funkcja wymaga podania e-maila i **hasła do eduVULCAN**.
> Są zapisywane w opcjach wpisu integracji (`.storage/core.config_entries`) **otwartym
> tekstem** — tak jak większość haseł w HA. Każdy, kto ma dostęp do plików konfiguracji
> HA albo do kopii zapasowych, może je odczytać.
>
> ⚠️ **Wiadomości mogą zostać oznaczone jako przeczytane.** Żeby wysłać treść nowej
> wiadomości, integracja otwiera ją tak jak przeglądarka. eduVULCAN może wtedy oznaczyć ją
> jako przeczytaną — także dla szkoły (nauczyciel zobaczy, że wiadomość została odczytana).
> Treść pobierana jest wyłącznie dla nowych wiadomości.

Od listopada 2026 API mobilne odpowiada na zapytania o wiadomości błędem
`111: EDUVULCAN_PREMIUM` (wiadomości tylko z płatnym eduVULCAN Premium). Moduł WWW
(`wiadomosci.eduvulcan.pl`), którego rodzice używają w przeglądarce, działa bez Premium,
więc integracja może czytać wiadomości stamtąd:

1. **Ustawienia → Urządzenia i usługi → eduVULCAN → Konfiguruj** (przy dowolnym wpisie)
2. Wpisz e-mail i hasło, którymi logujesz się na eduvulcan.pl
3. Wystarczy w jednym wpisie — dane są wspólne dla wszystkich dzieci z tego konta.
   Puste pola wyłączają moduł WWW.

Jak to działa: integracja loguje się na `eduvulcan.pl/logowanie`, otwiera
`eduvulcan.pl/dostep-do-dziennika/`, wchodzi w link dziecka (to otwiera sesję w jego
jednostce, np. `krakow`, `wieliczka`) i czyta `wiadomosci.eduvulcan.pl/<jednostka>/api/Odebrane`
oraz `.../api/WiadomoscSzczegoly`. W jednej sesji aktywna jest tylko jedna jednostka, więc
dzieci z różnych szkół obsługiwane są po kolei.

- **Captcha:** jeśli eduVULCAN zażąda captchy, integracja **nie próbuje jej rozwiązać** —
  przerywa, pokazuje zgłoszenie w HA (Ustawienia → Naprawy) i ponawia za kilka godzin.
  Zaloguj się wtedy raz w przeglądarce.
- **Złe hasło:** zgłoszenie w Naprawach; integracja nie próbuje ponownie, dopóki nie zmienisz
  danych (żeby nie zablokować konta).
- **Pierwsze uruchomienie** tylko zapamiętuje istniejące wiadomości — zdarzenia dostaniesz
  dla wiadomości, które przyjdą później.

### Zdarzenia

| Zdarzenie | Dane |
|---|---|
| `eduvulcan_message` | `kid`, `id`, `subject`, `text` (bez HTML), `sender`, `attachments` (liczba), `date`, `test` |
| `eduvulcan_announcement` | `kid`, `id`, `title`, `text`, `sender`, `category`, `valid_from`, `valid_to`, `attachments`, `date`, `test` |

Ta sama wiadomość wysłana do rodzeństwa wywołuje zdarzenie raz. Usługa
`eduvulcan.send_latest` wywołuje oba zdarzenia dla najnowszej wiadomości i ogłoszenia z
`test: true` (do sprawdzania automatyzacji). Ogłoszenia nadal idą przez API mobilne.

## Zmiany względem oryginalnej biblioteki iris

Pliki w `custom_components/eduvulcan/iris/` to kopia upstreamu z trzema rodzajami zmian
(każda oznaczona komentarzem `PATCHED for HA vendoring`):

- importy `from iris.*` przepisane na względne,
- adnotacje `any` (wbudowana funkcja) zamienione na `typing.Any` — zgodność z pydantic 2.13
  pinowanym przez HA Core,
- `HttpClient` dostał metodę `close()` (upstreamowy `__aexit__` odwoływał się do
  nieistniejącego pola).

## Licencja

AGPL-3.0 (dziedziczona po bibliotece iris).
