import { Page, Placeholder } from "../components/Page";

export function LibraryPage() {
  return (
    <Page title="Library" subtitle="Sonarr series and Radarr movies, with a Clean toggle each.">
      <Placeholder milestone="M4">
        Needs the Sonarr/Radarr sync from M3 before there is anything to list.
      </Placeholder>
    </Page>
  );
}
