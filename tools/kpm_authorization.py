"""Bind KPM visibility and command authorization to each native manager API."""
import re


def render_kpm_authorization(template: str, namespace: str,
                             authorization_expr: str | None = None,
                             command_expr: str | None = None) -> str:
    if not re.fullmatch(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+', namespace):
        raise ValueError('Expected the native manager Kotlin namespace')
    if (authorization_expr is None) != (command_expr is None):
        raise ValueError('Custom KPM authorization requires both UI and command gates')
    if authorization_expr is None:
        native = f'{namespace}.Natives.isManager && {namespace}.Natives.isFullFeatured()'
        # Native full-feature checks can create a root shell. Failure here is
        # an unavailable entry, not a reason to start the KPM daemon or a dialog.
        authorization_expr = 'kpmUiAuthorized { ' + native + ' }'
        command_expr = native
    if any(not isinstance(value, str) or not value.strip() or len(value) > 1024
           or any(character in value for character in ('\r', '\n', '\0'))
           for value in (authorization_expr, command_expr)):
        raise ValueError('Expected bounded native KPM authorization expressions')
    # CompositionLocal.current is a composable read. A command runs on IO and
    # must use the manager's fresh authorization API instead of that snapshot.
    if re.search(r'\bLocal\w+\.current\b', command_expr):
        raise ValueError('KPM commands cannot read a Compose authorization snapshot')
    for marker, expression in (
            ('__KPM_AUTHORIZATION__', authorization_expr),
            ('__KPM_COMMAND_AUTHORIZATION__', command_expr)):
        if template.count(marker) != 1:
            raise ValueError('Review the native KPM authorization integration point')
        template = template.replace(marker, expression)
    return template
