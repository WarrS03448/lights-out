'use strict';
// Only recovery copy; existing login/registration templates remain in account-mail.
const COPY={
  en:['Reset your Lights Out password','Your Lights Out password reset code is:',
    'Enter this code in Lights Out to verify your email before choosing a new password. It expires in 15 minutes. If you did not request this, ignore this email. Never share this code.',
    'Your Lights Out account request','There is no Lights Out account registered to this email address. You can create one in the Lights Out app or on the Lights Out website. If you did not request this email, ignore it.'],
  de:['Setze dein Lights Out-Passwort zurück','Dein Code zum Zurücksetzen des Lights Out-Passworts lautet:',
    'Gib diesen Code in Lights Out ein, um deine E-Mail zu bestätigen, bevor du ein neues Passwort wählst. Er läuft nach 15 Minuten ab. Wenn du dies nicht angefordert hast, ignoriere diese E-Mail. Teile den Code niemals.',
    'Deine Lights Out-Kontoanfrage','Für diese E-Mail-Adresse ist kein Lights Out-Konto registriert. Du kannst eines in der Lights Out-App oder auf der Website erstellen. Wenn du diese E-Mail nicht angefordert hast, ignoriere sie.'],
  es:['Restablece tu contraseña de Lights Out','Tu código para restablecer la contraseña de Lights Out es:',
    'Introduce este código en Lights Out para verificar tu correo antes de elegir una nueva contraseña. Caduca en 15 minutos. Si no lo solicitaste, ignora este correo. Nunca compartas el código.',
    'Tu solicitud de cuenta de Lights Out','No hay ninguna cuenta de Lights Out registrada con este correo. Puedes crear una en la aplicación o el sitio web de Lights Out. Si no solicitaste este correo, ignóralo.'],
  fr:['Réinitialisez votre mot de passe Lights Out','Votre code de réinitialisation du mot de passe Lights Out est :',
    'Saisissez ce code dans Lights Out pour vérifier votre adresse e-mail avant de choisir un nouveau mot de passe. Il expire dans 15 minutes. Si vous ne l’avez pas demandé, ignorez cet e-mail. Ne partagez jamais ce code.',
    'Votre demande de compte Lights Out','Aucun compte Lights Out n’est associé à cette adresse e-mail. Vous pouvez en créer un dans l’application ou sur le site Lights Out. Si vous n’avez pas demandé cet e-mail, ignorez-le.'],
  pt:['Redefina sua senha do Lights Out','Seu código para redefinir a senha do Lights Out é:',
    'Digite este código no Lights Out para verificar seu e-mail antes de escolher uma nova senha. Ele expira em 15 minutos. Se não fez esta solicitação, ignore este e-mail. Nunca compartilhe o código.',
    'Sua solicitação de conta Lights Out','Não há uma conta Lights Out registrada com este e-mail. Você pode criar uma no aplicativo ou no site Lights Out. Se não solicitou este e-mail, ignore-o.'],
  ru:['Сброс пароля Lights Out','Ваш код для сброса пароля Lights Out:',
    'Введите этот код в Lights Out, чтобы подтвердить почту перед выбором нового пароля. Код действует 15 минут. Если вы не запрашивали сброс, проигнорируйте письмо. Никому не сообщайте код.',
    'Запрос аккаунта Lights Out','На этот адрес почты не зарегистрирован аккаунт Lights Out. Вы можете создать его в приложении или на сайте Lights Out. Если вы не запрашивали это письмо, проигнорируйте его.'],
  zh:['重置 Lights Out 密码','你的 Lights Out 密码重置验证码是：',
    '请在 Lights Out 中输入此验证码，验证邮箱后再设置新密码。验证码在 15 分钟后失效。如果你没有提出此请求，请忽略此邮件。切勿与他人分享验证码。',
    '你的 Lights Out 账号请求','此邮箱尚未注册 Lights Out 账号。你可以在 Lights Out 应用或网站上创建账号。如果你没有请求此邮件，请忽略。'],
};
function recoveryMail({language,token,actionable}) {
  const c=Object.hasOwn(COPY,language)?COPY[language]:COPY.en;
  return actionable?{subject:c[0],text:c[1]+'\n\n'+token+'\n\n'+c[2]+'\n'}:{subject:c[3],text:c[4]+'\n'};
}
module.exports={recoveryMail};
