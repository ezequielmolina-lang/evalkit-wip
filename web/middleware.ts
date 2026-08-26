import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

/**
 * Access gate. Ported from the APD site's edge middleware, with one deliberate
 * difference: **this one fails closed.**
 *
 * APD opens the site when SITE_PASSWORD is unset, which is right for a gallery
 * of illustrative drafts. It is wrong here. An open evalkit lets anyone start
 * runs that drive a vendor's live platform, spend the owner's API budget, and
 * send vendor-facing requests under the owner's name. A missing password is a
 * misconfiguration, and the safe reading of a misconfiguration is "no".
 */

const COOKIE = "evalkit_ok";
const SALT = "evalkit|acceso-por-invitacion";

async function tokenFor(pass: string) {
  const data = new TextEncoder().encode(`${SALT}|${pass}`);
  const buf = await crypto.subtle.digest("SHA-256", data);
  return Array.from(new Uint8Array(buf))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

const SHELL = (body: string) => `<!DOCTYPE html><html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex, nofollow"><title>Acceso · evalkit</title>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,600&family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:22px;
  background:#0C3B2E;font-family:Inter,system-ui,sans-serif;color:#0C3B2E}
.card{background:#F4F1E8;border-radius:18px;width:100%;max-width:430px;padding:38px 34px;
  box-shadow:0 30px 80px rgba(0,0,0,.4)}
.mark{font-family:Fraunces,Georgia,serif;font-size:24px;font-weight:600;letter-spacing:-.02em}
.mark span{color:#16C48A}
.kick{font-size:10.5px;letter-spacing:.18em;text-transform:uppercase;color:#175946;font-weight:700;margin-top:16px}
h1{font-family:Fraunces,Georgia,serif;font-size:23px;font-weight:600;margin:7px 0 10px;line-height:1.2}
p.sub{font-size:14px;color:#5F6B62;margin-bottom:22px;line-height:1.6}
input{width:100%;font-size:15px;padding:12px 14px;border:1px solid #DDD8C8;border-radius:10px;
  margin-bottom:12px;outline:none;background:#fff;font-family:inherit}
input:focus{border-color:#16C48A;box-shadow:0 0 0 3px rgba(22,196,138,.16)}
button{width:100%;font-size:15px;font-weight:600;padding:13px;border:none;border-radius:999px;
  background:#0C3B2E;color:#F4F1E8;cursor:pointer;font-family:inherit}
button:hover{background:#114A3A}
.err{background:rgba(192,85,58,.1);color:#C0553A;font-size:13.5px;border-radius:9px;padding:11px;margin-bottom:12px}
.foot{font-size:11.5px;color:#8A9490;margin-top:20px;line-height:1.55}
</style></head><body>${body}</body></html>`;

const loginPage = (error?: string) =>
  SHELL(`<form class="card" method="POST" action="">
  <div class="mark">eval<span>kit</span></div>
  <div class="kick">Acceso por invitación</div>
  <h1>Este piloto es cerrado</h1>
  <p class="sub">evalkit ejecuta evaluaciones contra plataformas de terceros. El acceso está limitado a un grupo invitado.</p>
  ${error ? `<div class="err">${error}</div>` : ""}
  <input type="password" name="password" placeholder="Contraseña" autofocus required autocomplete="current-password">
  <button type="submit">Entrar</button>
  <div class="foot">Prototipo en desarrollo. No constituye una recomendación de compra.</div>
</form>`);

const misconfigured = () =>
  SHELL(`<div class="card">
  <div class="mark">eval<span>kit</span></div>
  <div class="kick">Configuración incompleta</div>
  <h1>El sitio está cerrado</h1>
  <p class="sub">No hay contraseña configurada, así que no se puede verificar quién entra. El sitio se cierra en lugar de abrirse: una corrida puede tocar la plataforma de un proveedor y gastar la clave de API del titular.</p>
  <div class="foot">Definí <code>SITE_PASSWORD</code> en el entorno para habilitar el acceso.</div>
</div>`);

const html = (body: string, status: number) =>
  new NextResponse(body, { status, headers: { "content-type": "text/html; charset=utf-8" } });

export const config = {
  // Everything except Next's own assets and the favicon.
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};

/**
 * Routes that stay public when PUBLIC_LANDING is set. The landing page exists
 * to explain and sell; behind a password it can do neither. But it is opt-in,
 * not the default: opening a page is a decision the owner should make on
 * purpose, not inherit from a config file they never edited.
 *
 * The app itself is never public — that is what spends the API budget and
 * touches vendor platforms.
 */
const PUBLIC_PATHS = new Set(["/"]);

export async function middleware(req: NextRequest) {
  // INSTALACIÓN LOCAL. La compuerta existe porque un evalkit abierto deja que
  // cualquiera lance corridas que gastan la API key del dueño y tocan la
  // plataforma de un proveedor. En una instalación local atada a 127.0.0.1 el
  // único que llega es el dueño, así que pedirle una contraseña a su propia
  // máquina es fricción sin seguridad: lo que protege ahí es la interfaz de
  // red, no un secreto compartido.
  //
  // Es OPT-IN y lo pone el lanzador (`evalkit_app.py`), que además ata el
  // servidor a 127.0.0.1. Un despliegue sigue fallando cerrado sin contraseña:
  // esta variable no se activa sola.
  if (process.env.EVALKIT_LOCAL === "1") {
    return NextResponse.next();
  }

  const pass = process.env.SITE_PASSWORD;

  if (process.env.PUBLIC_LANDING === "1" && PUBLIC_PATHS.has(req.nextUrl.pathname)) {
    return NextResponse.next();
  }

  // Fail closed. See the note at the top of this file.
  if (!pass) return html(misconfigured(), 503);

  const expected = await tokenFor(pass);
  if (req.cookies.get(COOKIE)?.value === expected) return NextResponse.next();

  if (req.method === "POST") {
    const form = await req.formData().catch(() => null);
    const given = String(form?.get("password") ?? "");
    if (given && (await tokenFor(given)) === expected) {
      const res = NextResponse.redirect(new URL(req.nextUrl.pathname, req.url), 303);
      res.cookies.set(COOKIE, expected, {
        httpOnly: true,
        sameSite: "lax",
        secure: req.nextUrl.protocol === "https:",
        path: "/",
        maxAge: 60 * 60 * 24 * 30,
      });
      return res;
    }
    return html(loginPage("Contraseña incorrecta."), 401);
  }

  return html(loginPage(), 401);
}
