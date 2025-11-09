# Guide de contribution

Merci de votre intérêt pour contribuer à **Microsoft Family Safety pour Home Assistant** ! 🎉

Cette intégration a **besoin de votre aide** pour résoudre des problèmes critiques et améliorer les fonctionnalités.

---

## 🚨 Problèmes prioritaires nécessitant votre aide

### 1. 🔐 Contrôle des appareils (CRITIQUE)

**Problème** : Le blocage/déblocage distant des appareils ne fonctionne pas.

**Contexte** :
- Les commandes API sont acceptées (status 201)
- L'état change dans Home Assistant
- **MAIS** les appareils ne se bloquent/débloquent pas réellement

**Ce dont nous avons besoin** :
- Analyse du trafic réseau de l'application mobile Microsoft Family Safety
- Reverse engineering des vrais endpoints de blocage
- Tests avec différentes méthodes HTTP (POST, PUT, PATCH)
- Documentation des payloads qui fonctionnent réellement

**Outils suggérés** :
- [mitmproxy](https://mitmproxy.org/) - Proxy HTTPS pour analyser le trafic
- [Burp Suite](https://portswigger.net/burp) - Suite complète d'analyse
- [Charles Proxy](https://www.charlesproxy.com/) - Alternative conviviale
- [Wireshark](https://www.wireshark.org/) - Analyse réseau

**Comment aider** :
1. Installer un proxy HTTPS sur votre appareil mobile
2. Utiliser l'app Microsoft Family Safety pour bloquer un appareil
3. Capturer le trafic HTTP/HTTPS
4. Documenter les endpoints, méthodes, headers et payloads utilisés
5. Partager vos découvertes dans une issue GitHub

---

### 2. 📚 Documentation de l'API non officielle

**Problème** : Microsoft ne fournit aucune documentation publique.

**Ce dont nous avons besoin** :
- Cartographie complète des endpoints disponibles
- Structure des requêtes et réponses
- Identification des limitations et quotas
- Documentation des codes d'erreur

**Comment aider** :
1. Tester différents endpoints de l'API
2. Documenter les résultats dans `/docs/api/`
3. Créer des exemples de requêtes curl
4. Ajouter des commentaires dans le code

---

### 3. 🔑 Amélioration de l'authentification

**Problème** : Le token doit être récupéré manuellement et expire régulièrement.

**Ce dont nous avons besoin** :
- Implémentation du refresh automatique du token
- Support complet du flux OAuth2
- Meilleure gestion des erreurs d'authentification

**Comment aider** :
1. Analyser le processus OAuth de Microsoft
2. Implémenter un système de refresh automatique
3. Améliorer le config flow dans `config_flow.py`
4. Ajouter des tests d'authentification

---

## 🛠️ Comment contribuer

### Prérequis

- Python 3.11+
- Home Assistant (version 2024.1.0+)
- Git
- Un compte Microsoft Family Safety avec des appareils de test

### Configuration de l'environnement de développement

1. **Forker le projet**
```bash
# Sur GitHub, cliquer sur "Fork"
```

2. **Cloner votre fork**
```bash
git clone https://github.com/VOTRE-USERNAME/HAFamilySafety_dev.git
cd HAFamilySafety_dev
```

3. **Créer une branche**
```bash
git checkout -b feature/ma-nouvelle-fonctionnalite
```

4. **Installer dans Home Assistant**
```bash
# Copier dans votre dossier custom_components
cp -r custom_components/microsoft_family_safety /config/custom_components/
```

5. **Redémarrer Home Assistant**

### Structure du projet

```
custom_components/microsoft_family_safety/
├── __init__.py           # Initialisation de l'intégration
├── manifest.json         # Métadonnées et dépendances
├── config_flow.py        # Flux de configuration
├── coordinator.py        # Coordination des mises à jour
├── sensor.py            # Entités sensor
├── const.py             # Constantes
└── translations/        # Traductions
    └── fr.json          # Français
```

### Standards de code

- **Style** : Suivre [PEP 8](https://pep8.org/)
- **Type hints** : Utiliser les annotations de type Python
- **Docstrings** : Documenter toutes les fonctions publiques
- **Logs** : Utiliser `_LOGGER.debug()` pour le débogage

**Exemple de code bien formaté** :
```python
async def async_block_platform(
    self,
    account_id: str,
    platform: OverrideTarget,
    duration_minutes: int | None = None
) -> None:
    """Block a platform for an account.

    Args:
        account_id: ID of the account
        platform: Platform to block (WINDOWS, MOBILE, XBOX)
        duration_minutes: Optional duration in minutes

    Raises:
        ValueError: If account not found
    """
    if account_id not in self._accounts:
        raise ValueError(f"Account {account_id} not found")

    _LOGGER.debug("Blocking platform %s for account %s", platform, account_id)
    # ... reste du code
```

### Tests

Pour tester vos modifications :

1. **Activer les logs debug**
```yaml
# configuration.yaml
logger:
  default: info
  logs:
    custom_components.microsoft_family_safety: debug
    pyfamilysafety: debug
```

2. **Redémarrer Home Assistant**

3. **Vérifier les logs**
```
Paramètres → Système → Journaux
```

### Soumettre votre contribution

1. **Commiter vos changements**
```bash
git add .
git commit -m "Add: Description claire de votre modification"
```

**Conventions de commit** :
- `Add:` - Nouvelle fonctionnalité
- `Fix:` - Correction de bug
- `Update:` - Amélioration d'une fonctionnalité existante
- `Docs:` - Documentation uniquement
- `Refactor:` - Refactoring sans changement fonctionnel

2. **Pousser vers votre fork**
```bash
git push origin feature/ma-nouvelle-fonctionnalite
```

3. **Créer une Pull Request**
- Aller sur GitHub
- Cliquer sur "New Pull Request"
- Décrire vos changements en détail
- Lier les issues concernées

---

## 📋 Checklist avant de soumettre

- [ ] Mon code suit les standards PEP 8
- [ ] J'ai ajouté des docstrings à mes fonctions
- [ ] J'ai testé mes modifications localement
- [ ] J'ai vérifié qu'il n'y a pas d'erreurs dans les logs
- [ ] J'ai mis à jour la documentation si nécessaire
- [ ] Mon commit a un message descriptif

---

## 🐛 Signaler un bug

Pour signaler un bug, [créez une issue](https://github.com/noiwid/HAFamilySafety_dev/issues/new) avec :

1. **Description claire** du problème
2. **Étapes pour reproduire** le bug
3. **Comportement attendu** vs comportement observé
4. **Environnement** :
   - Version de Home Assistant
   - Version de l'intégration
   - Système d'exploitation
5. **Logs pertinents** (avec debug activé)

**Template d'issue** :
```markdown
## Description
Décrivez le problème...

## Étapes pour reproduire
1. Aller dans...
2. Cliquer sur...
3. Observer...

## Comportement attendu
Ce qui devrait se passer...

## Comportement observé
Ce qui se passe réellement...

## Environnement
- Home Assistant: 2024.11.0
- Intégration: 1.0.0
- OS: Home Assistant OS

## Logs
```
[Collez vos logs ici]
```
```

---

## 💡 Proposer une nouvelle fonctionnalité

Pour proposer une fonctionnalité, [créez une issue](https://github.com/noiwid/HAFamilySafety_dev/issues/new) avec :

1. **Description** de la fonctionnalité
2. **Cas d'usage** : Pourquoi c'est utile ?
3. **Proposition d'implémentation** (optionnel)
4. **Alternatives envisagées** (optionnel)

---

## 🌍 Traductions

Les traductions sont dans `custom_components/microsoft_family_safety/translations/`.

Pour ajouter une langue :

1. Copier `fr.json` vers `VOTRE_LANGUE.json`
2. Traduire toutes les chaînes
3. Tester dans Home Assistant avec votre langue
4. Soumettre une PR

---

## 📞 Besoin d'aide ?

- **Questions générales** : [GitHub Discussions](https://github.com/noiwid/HAFamilySafety_dev/discussions)
- **Bugs** : [GitHub Issues](https://github.com/noiwid/HAFamilySafety_dev/issues)
- **Chat** : Créez une discussion pour obtenir de l'aide

---

## 📚 Ressources utiles

### Documentation Home Assistant
- [Developer Docs](https://developers.home-assistant.io/)
- [Integration Development](https://developers.home-assistant.io/docs/creating_component_index)
- [Architecture Decisions](https://developers.home-assistant.io/docs/architecture_index)

### Bibliothèques utilisées
- [pyfamilysafety](https://github.com/pantherale0/pyfamilysafety) - Client Python pour l'API
- [aiohttp](https://docs.aiohttp.org/) - HTTP client async

### Outils de développement
- [Home Assistant Development Container](https://developers.home-assistant.io/docs/development_environment)
- [VS Code + Home Assistant Extension](https://marketplace.visualstudio.com/items?itemName=keesschollaart.vscode-home-assistant)

---

## 🙏 Remerciements

Merci à tous les contributeurs qui rendent ce projet possible !

- **Mainteneur** : [@noiwid](https://github.com/noiwid)
- **Inspiré par** : [ha-familysafety](https://github.com/pantherale0/ha-familysafety) de [@pantherale0](https://github.com/pantherale0)

---

## 📄 Licence

En contribuant à ce projet, vous acceptez que vos contributions soient sous licence MIT.

---

**Merci de contribuer à améliorer Microsoft Family Safety pour Home Assistant ! 🎉**
