$(document).on('click', '.job-action', function(event){
	event.preventDefault();

  let $link = $(this);
	let url = $link.attr('href');

  if ( $link.hasClass('job-action-delete') ) {
    if ( !confirm('Are you sure you want to delete this job?') ) {
      return false;
    }
  }

  $.post(url, {}).done(function(message) {
    growl_success( typeof message == 'string' ? message : "Request succeeded" );

    if ( $link.hasClass('job-pause') ) {
      $link.closest('.dropdown').find('.job-resume').removeClass('d-none');
      $link.addClass('d-none');
    }
    else if ( $link.hasClass('job-resume') ) {
      $link.closest('.dropdown').find('.job-pause').removeClass('d-none');
      $link.addClass('d-none');
    }

    reload_page();
  }).fail(function(message) {
    growl_error( typeof message == 'string' ? message : "There was a problem with this request");
  });
}).on('click', '.run-action', function(event){
	let $button = $(this);
	let url = $button.data('url');

  let confirm_message = $button.data('confirm');
  if ( confirm_message ) {
    if ( !confirm(confirm_message) ) {
      return false;
    }
  }

  $.ajax({
      url: url,
      method: 'PUT'
  }).done(function(message) {
    growl_success( typeof message == 'string' ? message : "Request succeeded" );
  }).fail(function(message) {
    growl_error( typeof message == 'string' ? message : "There was a problem with this request");
  });
}).on('click', '.job-update', function(event){
	event.preventDefault();

  let $link = $(this);
	let url = $link.attr('href');

  let $modal = $('#update-job');
  $modal.find('.modal-content').load(url);
  $modal.modal('show');
}).on('click', '.add-job-button', function(event){
	event.preventDefault();

  let url = $(this).data('href');

  let $modal = $('#update-job');
  $modal.find('.modal-content').load(url);
  $modal.modal('show');
}).on('submit', '.job-form', function(event){
  event.preventDefault();
  let $form = $(this);

  var name = $.trim($('#name').val());
  var command = $.trim($('#command').val());
  var cron = $.trim($('#cron_schedule').val());
  var ttl = $.trim($('#ttl').val());

  if( name.length == 0 || command.length == 0 ) {
    growl_error("Please enter a name, a command, and a schedule");
    return false;
  }

  if ( ttl.length > 0 && !ttl.match(/^[0-9]+$/) ) {
    growl_error("Please enter an integer TTL value");
    return false;
  }

  // check name last - it's very important that it's valid since it will be used as a redis key
  if ( name.match(/^[a-z0-9\-_]+$/i)) {
    if ( cron.length == 0 || cron.match(/(@(annually|yearly|monthly|weekly|daily|hourly|reboot))|(@every (\d+(ns|us|µs|ms|s|m|h))+)|((((\d+,)+\d+|(\d+(\/|-)\d+)|\d+|\*) ?){5,7})/) ) {
      $.ajax({
        type: "POST",
        url: $form.attr('action'),
        data: $form.serialize()
      }).done(function () {
        reload_page();
      }).fail(function () {
        growl_error("There was a problem saving this job definition");
      });
    }
    else {
      growl_error("Please enter a valid cron schedule");
      return false;
    }
  }
  else {
    growl_error("Please enter a valid job name (no spaces allowed)");
    return false;
  }
}).on('click', '.job-output', function(event){
	let $button = $(this);
	let url = $button.data('url');
  let dialog_title = $button.data('dialog-title');

  let $modal = $('#job-output');
  $modal.find('.modal-title').text( dialog_title );
  $modal.find('.modal-body').load( url );
  $modal.modal('show');
});

function growl_error(message) {
  $.notify({
    message: message
  },{
    type: 'danger'
  });
}

function growl_success(message) {
  $.notify({
    message: message
  },{
    type: 'success'
  });
}

function reload_page(){
  window.location.reload();
}