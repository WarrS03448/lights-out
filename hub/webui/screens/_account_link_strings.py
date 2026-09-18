"""Account linking copy for the same seven languages as the desktop app."""
_KEYS = """link_button link_connected link_hint link_title link_login_intro link_email_code_intro link_steam_intro link_steam_button link_steam_waiting link_password_intro link_send_confirmation link_confirm_intro link_confirm_button link_success link_signin link_pair link_start_over link_progress_conflict link_owner_conflict link_match link_expired link_code_invalid link_restart link_credentials link_rate link_wrong_steam link_unavailable""".split()
_COPY = {
'en': [
'Connect Lights Out account', 'Lights Out account connected: {email}',
'Use either sign-in for the same player profile and progress.', 'Connect your accounts',
'Sign in to your existing Lights Out account. Your Steam session stays active during verification.',
'Enter the login code sent to your Lights Out email.',
'Confirm the same Steam account in your browser. This verification expires after five minutes.',
'Confirm Steam account', 'Waiting for Steam verification in your browser…',
'Confirm your Lights Out password to send the account-connection code.', 'Send connection code',
'Enter the new emailed code to connect these sign-ins. Existing progress is kept; two separate profiles with player data cannot be merged.',
'Connect accounts', 'Accounts connected. Sign in again with Steam or Lights Out to load your confirmed player profile.',
'Go to sign-in', 'Lights Out: {email} · Steam: {steam_id}', 'Start over',
'Both accounts have separate player data. We cannot combine their progress automatically. No connection was made.',
'One of these accounts is already connected or its ownership changed. No connection was made. Start over or contact support.',
'Finish your match before connecting accounts.', 'Steam verification expired. Start over to confirm Steam again.',
'This code is incorrect or expired. Check the code, or start over for a new one.',
'Your sign-in changed or expired. Start over to verify it again.', 'Check your email and password and try again.',
'Too many attempts. Wait a few minutes before trying again.',
'The browser confirmed a different Steam account. Confirm the Steam account currently signed in to Lights Out.',
'Account connection is temporarily unavailable. Please try again later.'
],
'de': [
'Lights Out-Konto verbinden', 'Lights Out-Konto verbunden: {email}',
'Nutze beide Anmeldungen für dasselbe Spielerprofil und denselben Fortschritt.', 'Konten verbinden',
'Melde dich bei deinem bestehenden Lights Out-Konto an. Deine Steam-Sitzung bleibt während der Prüfung aktiv.',
'Gib den Anmeldecode ein, der an deine Lights Out-E-Mail gesendet wurde.',
'Bestätige dasselbe Steam-Konto im Browser. Die Bestätigung läuft nach fünf Minuten ab.',
'Steam-Konto bestätigen', 'Warte auf die Steam-Bestätigung im Browser…',
'Bestätige dein Lights Out-Passwort, um den Verbindungscode zu senden.', 'Verbindungscode senden',
'Gib den neuen E-Mail-Code ein, um diese Anmeldungen zu verbinden. Fortschritt bleibt erhalten; zwei Profile mit eigenen Spielerdaten können nicht zusammengeführt werden.',
'Konten verbinden', 'Konten verbunden. Melde dich erneut mit Steam oder Lights Out an, um dein bestätigtes Spielerprofil zu laden.',
'Zur Anmeldung', 'Lights Out: {email} · Steam: {steam_id}', 'Neu beginnen',
'Beide Konten haben eigene Spielerdaten. Ihr Fortschritt kann nicht automatisch zusammengeführt werden. Es wurde keine Verbindung hergestellt.',
'Eines der Konten ist bereits verbunden oder der Besitz hat sich geändert. Keine Verbindung hergestellt. Beginne neu oder kontaktiere den Support.',
'Beende dein Match, bevor du Konten verbindest.', 'Die Steam-Bestätigung ist abgelaufen. Beginne neu und bestätige Steam erneut.',
'Dieser Code ist falsch oder abgelaufen. Prüfe ihn oder beginne neu für einen neuen Code.',
'Deine Anmeldung hat sich geändert oder ist abgelaufen. Beginne die Prüfung erneut.', 'Prüfe E-Mail und Passwort und versuche es erneut.',
'Zu viele Versuche. Warte einige Minuten und versuche es erneut.',
'Im Browser wurde ein anderes Steam-Konto bestätigt. Bestätige das Konto, das gerade bei Lights Out angemeldet ist.',
'Die Kontoverbindung ist vorübergehend nicht verfügbar. Versuche es später erneut.'
],
'es': [
'Conectar cuenta de Lights Out', 'Cuenta de Lights Out conectada: {email}',
'Usa cualquiera de los dos accesos para el mismo perfil y progreso.', 'Conectar tus cuentas',
'Inicia sesión en tu cuenta existente de Lights Out. Tu sesión de Steam seguirá activa durante la verificación.',
'Introduce el código de acceso enviado a tu correo de Lights Out.',
'Confirma la misma cuenta de Steam en el navegador. La verificación caduca en cinco minutos.',
'Confirmar cuenta de Steam', 'Esperando la verificación de Steam en el navegador…',
'Confirma tu contraseña de Lights Out para enviar el código de conexión.', 'Enviar código de conexión',
'Introduce el nuevo código del correo para conectar estos accesos. Se conserva el progreso; no se pueden fusionar dos perfiles con datos propios.',
'Conectar cuentas', 'Cuentas conectadas. Vuelve a entrar con Steam o Lights Out para cargar tu perfil confirmado.',
'Ir al inicio de sesión', 'Lights Out: {email} · Steam: {steam_id}', 'Empezar de nuevo',
'Ambas cuentas tienen datos propios. No podemos combinar su progreso automáticamente. No se han conectado.',
'Una cuenta ya está conectada o su titularidad ha cambiado. No se han conectado. Empieza de nuevo o contacta con soporte.',
'Termina la partida antes de conectar las cuentas.', 'La verificación de Steam ha caducado. Empieza de nuevo para confirmar Steam.',
'El código es incorrecto o ha caducado. Revísalo o empieza de nuevo para obtener otro.',
'Tu sesión ha cambiado o caducado. Empieza de nuevo para verificarla.', 'Revisa el correo y la contraseña e inténtalo de nuevo.',
'Demasiados intentos. Espera unos minutos antes de volver a intentarlo.',
'El navegador confirmó otra cuenta de Steam. Confirma la cuenta con la que has entrado en Lights Out.',
'La conexión de cuentas no está disponible temporalmente. Inténtalo más tarde.'
],
'fr': [
'Connecter un compte Lights Out', 'Compte Lights Out connecté : {email}',
'Utilise les deux connexions pour le même profil et la même progression.', 'Connecter vos comptes',
'Connectez-vous à votre compte Lights Out existant. Votre session Steam reste active pendant la vérification.',
'Saisissez le code de connexion envoyé à votre adresse Lights Out.',
'Confirmez le même compte Steam dans le navigateur. Cette vérification expire après cinq minutes.',
'Confirmer le compte Steam', 'En attente de la vérification Steam dans le navigateur…',
'Confirmez votre mot de passe Lights Out pour envoyer le code de connexion des comptes.', 'Envoyer le code',
'Saisissez le nouveau code reçu par e-mail pour connecter ces accès. La progression est conservée ; deux profils ayant leurs propres données ne peuvent pas être fusionnés.',
'Connecter les comptes', 'Comptes connectés. Reconnectez-vous avec Steam ou Lights Out pour charger votre profil confirmé.',
'Aller à la connexion', 'Lights Out : {email} · Steam : {steam_id}', 'Recommencer',
'Les deux comptes ont leurs propres données. Nous ne pouvons pas fusionner automatiquement leurs progressions. Aucune connexion effectuée.',
'Un compte est déjà connecté ou son propriétaire a changé. Aucune connexion effectuée. Recommencez ou contactez le support.',
'Terminez votre partie avant de connecter les comptes.', 'La vérification Steam a expiré. Recommencez pour confirmer Steam.',
'Ce code est incorrect ou expiré. Vérifiez-le ou recommencez pour en recevoir un autre.',
'Votre connexion a changé ou expiré. Recommencez la vérification.', 'Vérifiez votre adresse e-mail et votre mot de passe, puis réessayez.',
'Trop de tentatives. Attendez quelques minutes avant de réessayer.',
'Le navigateur a confirmé un autre compte Steam. Confirmez le compte actuellement connecté à Lights Out.',
'La connexion des comptes est temporairement indisponible. Réessayez plus tard.'
],
'pt': [
'Conectar conta Lights Out', 'Conta Lights Out conectada: {email}',
'Use qualquer uma das entradas para o mesmo perfil e progresso.', 'Conectar suas contas',
'Entre na sua conta Lights Out existente. Sua sessão Steam permanece ativa durante a verificação.',
'Digite o código de entrada enviado ao e-mail da sua conta Lights Out.',
'Confirme a mesma conta Steam no navegador. A verificação expira em cinco minutos.',
'Confirmar conta Steam', 'Aguardando a verificação Steam no navegador…',
'Confirme sua senha Lights Out para enviar o código de conexão.', 'Enviar código de conexão',
'Digite o novo código enviado por e-mail para conectar as entradas. O progresso é mantido; dois perfis com dados próprios não podem ser mesclados.',
'Conectar contas', 'Contas conectadas. Entre novamente com Steam ou Lights Out para carregar seu perfil confirmado.',
'Ir para a entrada', 'Lights Out: {email} · Steam: {steam_id}', 'Recomeçar',
'As duas contas possuem dados próprios. Não podemos combinar seus progressos automaticamente. Nenhuma conexão foi feita.',
'Uma conta já está conectada ou seu proprietário mudou. Nenhuma conexão foi feita. Recomece ou fale com o suporte.',
'Termine sua partida antes de conectar contas.', 'A verificação Steam expirou. Recomece para confirmar o Steam.',
'Este código está incorreto ou expirou. Confira ou recomece para receber outro.',
'Sua sessão mudou ou expirou. Recomece para verificá-la.', 'Confira seu e-mail e senha e tente novamente.',
'Muitas tentativas. Espere alguns minutos antes de tentar novamente.',
'O navegador confirmou outra conta Steam. Confirme a conta atualmente conectada ao Lights Out.',
'A conexão de contas está temporariamente indisponível. Tente novamente mais tarde.'
],
'ru': [
'Подключить аккаунт Lights Out', 'Аккаунт Lights Out подключён: {email}',
'Используйте любой способ входа для одного профиля и прогресса.', 'Подключение аккаунтов',
'Войдите в существующий аккаунт Lights Out. Сеанс Steam останется активным во время проверки.',
'Введите код входа, отправленный на почту аккаунта Lights Out.',
'Подтвердите тот же аккаунт Steam в браузере. Проверка действует пять минут.',
'Подтвердить аккаунт Steam', 'Ожидаем подтверждения Steam в браузере…',
'Подтвердите пароль Lights Out, чтобы отправить код подключения аккаунтов.', 'Отправить код подключения',
'Введите новый код из письма, чтобы подключить способы входа. Прогресс сохраняется; два профиля с отдельными данными нельзя объединить.',
'Подключить аккаунты', 'Аккаунты подключены. Войдите снова через Steam или Lights Out, чтобы загрузить подтверждённый профиль.',
'Перейти ко входу', 'Lights Out: {email} · Steam: {steam_id}', 'Начать заново',
'У обоих аккаунтов есть отдельные данные. Мы не можем автоматически объединить прогресс. Подключение не выполнено.',
'Один из аккаунтов уже подключён или его владелец изменился. Подключение не выполнено. Начните заново или обратитесь в поддержку.',
'Завершите матч перед подключением аккаунтов.', 'Проверка Steam истекла. Начните заново и подтвердите Steam.',
'Код неверный или просрочен. Проверьте его или начните заново, чтобы получить новый.',
'Вход изменился или истёк. Начните проверку заново.', 'Проверьте почту и пароль и повторите попытку.',
'Слишком много попыток. Подождите несколько минут.',
'В браузере подтверждён другой аккаунт Steam. Подтвердите аккаунт, с которым вы сейчас вошли в Lights Out.',
'Подключение аккаунтов временно недоступно. Попробуйте позже.'
],
'zh': [
'连接 Lights Out 账户', '已连接 Lights Out 账户：{email}',
'使用任一登录方式访问同一玩家资料和进度。', '连接你的账户',
'登录你现有的 Lights Out 账户。验证期间，你的 Steam 会话将保持登录。',
'输入发送到 Lights Out 账户邮箱的登录验证码。',
'在浏览器中确认同一个 Steam 账户。此验证在五分钟后过期。',
'确认 Steam 账户', '正在等待浏览器中的 Steam 验证…',
'确认你的 Lights Out 密码以发送账户连接验证码。', '发送连接验证码',
'输入新收到的邮件验证码来连接这些登录方式。现有进度会保留；两个已有各自玩家数据的资料无法合并。',
'连接账户', '账户已连接。请重新通过 Steam 或 Lights Out 登录，以加载已确认的玩家资料。',
'前往登录', 'Lights Out：{email} · Steam：{steam_id}', '重新开始',
'两个账户都有各自的玩家数据，无法自动合并进度。账户未连接。',
'其中一个账户已连接或所有权已变更。账户未连接。请重新开始或联系支持。',
'请先结束比赛，再连接账户。', 'Steam 验证已过期。请重新开始以确认 Steam。',
'此验证码不正确或已过期。请检查验证码，或重新开始以获取新的验证码。',
'你的登录状态已变更或过期。请重新开始验证。', '请检查邮箱和密码后重试。',
'尝试次数过多。请等待几分钟再试。',
'浏览器确认了另一个 Steam 账户。请确认当前用于登录 Lights Out 的 Steam 账户。',
'账户连接暂时不可用。请稍后再试。'
]}
assert all(len(values)==len(_KEYS) for values in _COPY.values())
STRINGS={lang:dict(zip(_KEYS,values)) for lang,values in _COPY.items()}

STRINGS['en']["link_uncertain"] = 'We could not confirm whether the connection completed. Sign in again with Steam or Lights Out and check Settings before retrying.'
STRINGS['de']["link_uncertain"] = 'Wir konnten nicht bestätigen, ob die Verbindung abgeschlossen wurde. Melde dich erneut mit Steam oder Lights Out an und prüfe die Einstellungen, bevor du es erneut versuchst.'
STRINGS['es']["link_uncertain"] = 'No hemos podido confirmar si se completó la conexión. Vuelve a entrar con Steam o Lights Out y revisa Ajustes antes de intentarlo de nuevo.'
STRINGS['fr']["link_uncertain"] = 'Nous ne pouvons pas confirmer si la connexion des comptes a abouti. Reconnectez-vous avec Steam ou Lights Out et vérifiez les paramètres avant de réessayer.'
STRINGS['pt']["link_uncertain"] = 'Não foi possível confirmar se a conexão foi concluída. Entre novamente com Steam ou Lights Out e confira as configurações antes de tentar de novo.'
STRINGS['ru']["link_uncertain"] = 'Не удалось подтвердить результат подключения. Войдите снова через Steam или Lights Out и проверьте настройки перед повторной попыткой.'
STRINGS['zh']["link_uncertain"] = '无法确认账户连接是否已完成。请重新通过 Steam 或 Lights Out 登录，并在重试前检查设置。'

STRINGS['en']["link_idle_required"] = 'Leave your party and finish or cancel matchmaking before connecting accounts.'
STRINGS['de']["link_idle_required"] = 'Verlasse deine Gruppe und beende oder brich die Spielsuche ab, bevor du Konten verbindest.'
STRINGS['es']["link_idle_required"] = 'Sal del grupo y termina o cancela la búsqueda de partida antes de conectar cuentas.'
STRINGS['fr']["link_idle_required"] = 'Quittez votre groupe et terminez ou annulez la recherche de partie avant de connecter les comptes.'
STRINGS['pt']["link_idle_required"] = 'Saia do grupo e conclua ou cancele a busca de partida antes de conectar contas.'
STRINGS['ru']["link_idle_required"] = 'Покиньте группу и завершите или отмените поиск матча перед подключением аккаунтов.'
STRINGS['zh']["link_idle_required"] = '请先离开队伍并完成或取消匹配，再连接账户。'
