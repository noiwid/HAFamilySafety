# Microsoft Family Safety pour Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/release/noiwid/HAFamilySafety_dev.svg)](https://github.com/noiwid/HAFamilySafety_dev/releases)
[![License](https://img.shields.io/github/license/noiwid/HAFamilySafety_dev.svg)](LICENSE)

Intégration **en lecture seule** pour Home Assistant permettant de surveiller l'utilisation des appareils via Microsoft Family Safety.

---

## ⚠️ LIMITATIONS IMPORTANTES

### Ce qui fonctionne ✅

- **Surveillance du temps d'écran** : Temps utilisé aujourd'hui, moyenne quotidienne
- **Dernière utilisation** : Date et heure de dernière connexion des appareils
- **Informations de compte** : Prénom, nom, photo de profil
- **Solde du compte** : Argent de poche disponible (si activé)
- **Liste des appareils** : Tous les appareils associés aux comptes enfants
- **Données d'applications** : Applications installées et leur statut

### Ce qui NE fonctionne PAS ❌

**Le contrôle distant des appareils (blocage/déblocage) ne fonctionne pas.**

- Les commandes sont acceptées par l'API Microsoft
- L'état change dans Home Assistant
- **MAIS les appareils ne se bloquent/débloquent pas réellement**

### Pourquoi cette limitation ?

Ceci est une **limitation de Microsoft Family Safety**, pas de cette intégration :

1. **Pas d'API officielle** : Microsoft ne fournit aucune API publique pour Family Safety
2. **Fonctions désactivées** : Le bouton "Verrouiller l'appareil" dans l'application officielle Microsoft ne fonctionne plus
3. **Intégration originale abandonnée** : L'intégration [ha-familysafety](https://github.com/pantherale0/ha-familysafety) de pantherale0 a été archivée en octobre 2025, probablement pour les mêmes raisons

Cette intégration utilise une **API non documentée et non officielle** découverte par rétro-ingénierie. Microsoft peut la modifier ou la désactiver à tout moment.

---

## 📊 Fonctionnalités

### Capteurs disponibles

Pour chaque compte enfant surveillé :

- **Temps d'écran aujourd'hui** (`sensor.{prenom}_today_screentime`)
- **Temps d'écran moyen** (`sensor.{prenom}_average_screentime`)
- **Solde du compte** (`sensor.{prenom}_account_balance`)
- **Informations du compte** (`sensor.{prenom}_account_info`)

Pour chaque appareil :

- **Temps utilisé aujourd'hui** (`sensor.{device}_today_time_used`)
- **Dernière utilisation** (`sensor.{device}_last_seen`)
- **Informations de l'appareil** (`sensor.{device}_info`)

---

## 🚀 Installation

### Via HACS (Recommandé)

1. Ouvrez HACS dans Home Assistant
2. Cliquez sur **Intégrations**
3. Cliquez sur le menu **⋮** en haut à droite
4. Sélectionnez **Dépôts personnalisés**
5. Ajoutez l'URL : `https://github.com/noiwid/HAFamilySafety_dev`
6. Catégorie : **Integration**
7. Cliquez sur **Ajouter**
8. Recherchez "Microsoft Family Safety" dans HACS
9. Cliquez sur **Télécharger**
10. Redémarrez Home Assistant

### Installation manuelle

1. Téléchargez la dernière release depuis [GitHub](https://github.com/noiwid/HAFamilySafety_dev/releases)
2. Extrayez le dossier `custom_components/microsoft_family_safety` dans votre dossier `config/custom_components/`
3. Redémarrez Home Assistant

---

## ⚙️ Configuration

### 1. Obtenir le token Microsoft

1. Rendez-vous sur [https://familysafety.microsoft.com](https://familysafety.microsoft.com)
2. Connectez-vous avec votre compte Microsoft (le compte parent)
3. Ouvrez les **Outils de développement** de votre navigateur (F12)
4. Allez dans l'onglet **Réseau** (Network)
5. Actualisez la page
6. Recherchez une requête vers `mobileaggregator.family.microsoft.com`
7. Cliquez dessus et allez dans l'onglet **Headers**
8. Trouvez le header **Cookie** et copiez la valeur de `wl_at`

**Exemple de cookie** :
```
wl_at=3.1.0.0.3f20fbf...
```

### 2. Configurer l'intégration

1. Allez dans **Paramètres** → **Appareils et services**
2. Cliquez sur **+ Ajouter une intégration**
3. Recherchez "Microsoft Family Safety"
4. Collez votre token `wl_at`
5. Validez

L'intégration va automatiquement découvrir tous les comptes enfants et leurs appareils.

---

## 📖 Utilisation

### Exemples d'automatisations

#### Notification si temps d'écran dépassé

```yaml
automation:
  - alias: "Alerte temps d'écran excessif"
    trigger:
      - platform: numeric_state
        entity_id: sensor.firstname_today_screentime
        above: 120  # 2 heures
    action:
      - service: notify.mobile_app
        data:
          title: "⚠️ Temps d'écran"
          message: "Firstname a dépassé 2h de temps d'écran aujourd'hui"
```

#### Tableau de bord temps d'écran

```yaml
type: entities
title: Temps d'écran famille
entities:
  - entity: sensor.firstname_today_screentime
    name: Firstname aujourd'hui
  - entity: sensor.firstname_average_screentime
    name: Firstname moyenne
```

#### Graphique d'évolution

```yaml
type: history-graph
title: Évolution du temps d'écran
entities:
  - sensor.firstname_today_screentime
hours_to_show: 168  # 7 jours
```

---

## 🔧 Dépannage

### Le token expire

Le token `wl_at` expire après quelques semaines/mois. Si vous voyez des erreurs d'authentification :

1. Récupérez un nouveau token (voir Configuration)
2. Allez dans **Paramètres** → **Appareils et services** → **Microsoft Family Safety**
3. Cliquez sur **Configurer** et collez le nouveau token

### Les données ne se mettent pas à jour

- L'intégration interroge l'API toutes les **5 minutes**
- Vous pouvez forcer une mise à jour : **Paramètres** → **Appareils et services** → **Microsoft Family Safety** → **⋮** → **Recharger**

### Logs de debug

Pour activer les logs détaillés, ajoutez dans `configuration.yaml` :

```yaml
logger:
  default: info
  logs:
    custom_components.microsoft_family_safety: debug
    pyfamilysafety: debug
```

Puis redémarrez Home Assistant et consultez les logs dans **Paramètres** → **Système** → **Journaux**.

---

## 🤝 Contribuer

**Cette intégration a besoin de votre aide !**

### Problèmes connus nécessitant des contributeurs

#### 1. Contrôle des appareils 🔐

Le blocage/déblocage distant ne fonctionne pas actuellement.

**Ce qu'il faut faire** :
- Analyse du trafic réseau de l'application mobile Microsoft Family Safety
- Reverse engineering des endpoints API pour le blocage
- Tests de différentes structures de payload
- Documentation de l'API non officielle

**Compétences nécessaires** :
- Python
- Analyse réseau (Wireshark, mitmproxy, Charles Proxy)
- Reverse engineering d'API REST
- Tests et débogage

#### 2. Documentation de l'API 📚

L'API Microsoft Family Safety n'est pas documentée publiquement.

**Ce qu'il faut faire** :
- Cartographie complète des endpoints disponibles
- Documentation des structures de requêtes/réponses
- Identification des limitations et quotas
- Création d'une documentation technique complète

**Compétences nécessaires** :
- Rédaction technique
- Analyse d'API
- Python (pour tests)

#### 3. Amélioration de l'authentification 🔑

Le système actuel nécessite de récupérer manuellement le token.

**Ce qu'il faut faire** :
- Implémentation du refresh automatique du token
- Support complet du flux OAuth2
- Amélioration de la gestion des erreurs d'authentification
- Documentation du processus d'authentification

**Compétences nécessaires** :
- OAuth2 / JWT
- Python
- Home Assistant config flow
- Sécurité

### Comment contribuer ?

1. **Fork** le projet
2. **Créez une branche** pour votre fonctionnalité (`git checkout -b feature/AmazingFeature`)
3. **Committez** vos changements (`git commit -m 'Add some AmazingFeature'`)
4. **Poussez** vers la branche (`git push origin feature/AmazingFeature`)
5. **Ouvrez une Pull Request**

Pour plus de détails, consultez [CONTRIBUTING.md](CONTRIBUTING.md).

### Ressources utiles

- [Documentation Home Assistant Developer](https://developers.home-assistant.io/)
- [pyfamilysafety library](https://github.com/pantherale0/pyfamilysafety)
- [Home Assistant Integration Blueprint](https://github.com/custom-components/blueprint)
- [Burp Suite / Charles Proxy](https://portswigger.net/burp) pour l'analyse réseau

---

## 📝 Licence

Ce projet est distribué sous licence MIT. Voir [LICENSE](LICENSE) pour plus d'informations.

---

## ⚖️ Avertissement

Cette intégration utilise une **API non officielle** de Microsoft Family Safety. Elle n'est ni approuvée ni supportée par Microsoft.

- ⚠️ Microsoft peut modifier ou désactiver l'API à tout moment
- ⚠️ L'utilisation se fait à vos risques et périls
- ⚠️ Aucune garantie de fonctionnement n'est fournie
- ⚠️ Utilisez cette intégration de manière responsable et conforme aux conditions d'utilisation de Microsoft

---

## 🙏 Remerciements

- **[pantherale0](https://github.com/pantherale0)** pour l'intégration originale [ha-familysafety](https://github.com/pantherale0/ha-familysafety) et la bibliothèque [pyfamilysafety](https://github.com/pantherale0/pyfamilysafety)
- La communauté **Home Assistant** pour le support et les retours
- **noiwid** pour l'assistance au développement

---

## 📞 Support

- **Issues** : [GitHub Issues](https://github.com/noiwid/HAFamilySafety_dev/issues)
- **Discussions** : [GitHub Discussions](https://github.com/noiwid/HAFamilySafety_dev/discussions)
- **Forum Home Assistant** : [Community Forum](https://community.home-assistant.io/)

### Informations utiles pour le support

Lorsque vous signalez un problème, merci de fournir :
- Version de Home Assistant
- Version de l'intégration Microsoft Family Safety
- Logs pertinents (avec niveau debug activé)
- Description détaillée du problème
- Étapes pour reproduire

---

**Fait avec ❤️ pour la communauté Home Assistant**

> Si cette intégration vous est utile, pensez à lui donner une ⭐ sur GitHub !
